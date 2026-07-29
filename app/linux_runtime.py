from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import IO

from app.config import Settings


X11_SOCKET_DIR = Path("/tmp/.X11-unix")

_ACTIVE_RUNTIME: "LinuxRuntime | None" = None


def set_active_runtime(runtime: "LinuxRuntime | None") -> None:
    global _ACTIVE_RUNTIME
    _ACTIVE_RUNTIME = runtime


def active_runtime() -> "LinuxRuntime | None":
    """当前进程正在守护的浏览器运行时；未托管浏览器时返回 None。"""
    return _ACTIVE_RUNTIME


def browser_runtime_ready() -> bool:
    runtime = active_runtime()
    return runtime is None or runtime.ready


class MissingBrowserRuntime(RuntimeError):
    """缺少 Xvfb/Chromium 等必备可执行文件，属于无法自愈的配置错误。"""


class LinuxRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.processes: list[subprocess.Popen[bytes]] = []
        self.log_file: IO[bytes] | None = None
        self.generation = 0
        self.restart_count = 0
        self.last_error: str | None = None
        self._xvfb: subprocess.Popen[bytes] | None = None
        self._chrome: subprocess.Popen[bytes] | None = None
        self._vnc: subprocess.Popen[bytes] | None = None
        self._websockify: subprocess.Popen[bytes] | None = None
        self._vnc_command: list[str] | None = None
        self._websockify_command: list[str] | None = None
        self._owns_display = False
        self._binaries: dict[str, str] = {}
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._supervisor: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    @staticmethod
    def _find_binary(*names: str) -> str | None:
        return next((path for name in names if (path := shutil.which(name))), None)

    def _display_number(self) -> int:
        match = re.match(r"^:(\d+)", str(self.settings.linux_display).strip())
        if not match:
            raise RuntimeError(f"无法解析 DISPLAY 序号：{self.settings.linux_display}")
        return int(match.group(1))

    def _x_lock_path(self) -> Path:
        return Path(f"/tmp/.X{self._display_number()}-lock")

    def _x_socket_path(self) -> Path:
        return X11_SOCKET_DIR / f"X{self._display_number()}"

    def _x_server_ready(self) -> bool:
        """真正连一下 X 套接字，而不是相信残留的锁文件。"""
        if not hasattr(socket, "AF_UNIX"):
            return False
        socket_path = str(self._x_socket_path()).encode()
        # 抽象命名空间优先：Xvfb 会同时监听抽象套接字与文件套接字。
        for address in (b"\0" + socket_path, socket_path):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.5)
                    probe.connect(address)
                    return True
            except OSError:
                continue
        return False

    def _clear_stale_x_locks(self) -> bool:
        """清理上一次容器运行遗留的 /tmp/.X<n>-lock 与套接字。

        ``restart: unless-stopped`` 重启的是同一个容器，可写层里的 /tmp 不会重置。
        锁文件里记录的 PID 在新的 PID 命名空间中几乎必然被别的进程复用，X 服务器
        的 ``kill(pid, 0)`` 存活探测因此误判，直接以
        "Server is already active for display" 退出，导致永久重启循环。
        """
        if self._x_server_ready():
            return False
        removed = False
        for path in (self._x_lock_path(), self._x_socket_path()):
            try:
                if path.exists() or path.is_symlink():
                    path.unlink(missing_ok=True)
                    removed = True
            except OSError as exc:
                raise RuntimeError(f"无法清理 X11 残留锁 {path}：{exc}") from exc
        if removed:
            self._note(f"已清理 X11 残留锁：{self._x_lock_path()}")
        return removed

    def _clear_stale_chromium_locks(self) -> None:
        """Remove process-scoped locks left behind by a stopped container."""
        for name in (
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
            "DevToolsActivePort",
        ):
            path = self.settings.browser_data_dir / name
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(f"无法清理 Chromium 临时锁 {path}：{exc}") from exc

    # ------------------------------------------------------------------
    # 运行日志
    # ------------------------------------------------------------------

    def _open_log(self) -> None:
        path = self.settings.data_dir / "linux-runtime.log"
        # O_APPEND 让父子进程共享写入位置，截断轮转后不会留下稀疏空洞。
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o644)
        self.log_file = os.fdopen(descriptor, "wb", buffering=0)

    def _note(self, message: str) -> None:
        if self.log_file is None:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.log_file.write(f"[runtime {stamp}] {message}\n".encode("utf-8"))
        except OSError:
            return

    def _rotate_log_if_oversized(self) -> None:
        limit = max(0, int(self.settings.linux_runtime_log_max_bytes))
        if not limit or self.log_file is None:
            return
        try:
            if os.fstat(self.log_file.fileno()).st_size <= limit:
                return
            os.ftruncate(self.log_file.fileno(), 0)
        except OSError:
            return
        self._note(f"运行日志超过 {limit} 字节，已就地截断")

    def _log_tail(self, limit: int = 5000) -> str:
        path = self.settings.data_dir / "linux-runtime.log"
        try:
            with path.open("rb") as file:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(max(0, size - limit), os.SEEK_SET)
                return file.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # 进程管理
    # ------------------------------------------------------------------

    def _spawn(self, command: list[str]) -> subprocess.Popen[bytes]:
        assert self.log_file is not None
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(process)
        return process

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes] | None, timeout: float = 5) -> None:
        """结束整个进程组，避免 Chromium 的 zygote/renderer 变成孤儿进程。"""
        if process is None or process.poll() is not None:
            if process is not None:
                try:
                    process.wait(timeout=0.1)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            return
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        try:
            if killpg is not None and getpgid is not None:
                killpg(getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if killpg is not None and getpgid is not None:
                killpg(getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def _forget(self, process: subprocess.Popen[bytes] | None) -> None:
        if process is None:
            return
        try:
            self.processes.remove(process)
        except ValueError:
            pass

    def _build_chrome_command(self, chrome: str) -> list[str]:
        command = [
            chrome,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.settings.linux_cdp_port}",
            f"--user-data-dir={self.settings.browser_data_dir}",
            "--disable-dev-shm-usage",
            "--disable-notifications",
            "--keep-alive-for-test",
            "--password-store=basic",
            "--no-first-run",
            "--no-default-browser-check",
            # Xvfb 下没有窗口管理器，窗口会被判定为不可见并降频，长时间扫描会假死。
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            # 不再拉起 crashpad 常驻进程，避免长期运行堆积。
            "--disable-breakpad",
            "--disable-crash-reporter",
            # 限制磁盘缓存，防止 browser_data 卷在数周内被写满。
            f"--disk-cache-size={max(1, self.settings.linux_chromium_disk_cache_mb) * 1024 * 1024}",
            f"--media-cache-size={max(1, self.settings.linux_chromium_disk_cache_mb) * 1024 * 1024}",
            "--window-size=1440,940",
            "about:blank",
        ]
        if self.settings.linux_chromium_no_sandbox or (
            hasattr(os, "geteuid") and os.geteuid() == 0
        ):
            command.insert(1, "--no-sandbox")
        return command

    def _wait_for_x_server(self, process: subprocess.Popen[bytes], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                raise RuntimeError("服务正在停止，放弃等待 X 服务")
            if process.poll() is not None:
                raise RuntimeError(f"Xvfb 提前退出，退出码 {process.returncode}")
            if self._x_server_ready():
                return
            time.sleep(0.1)
        raise RuntimeError(f"等待 X 服务 {self.settings.linux_display} 就绪超时")

    def _wait_for_port(
        self,
        port: int,
        process: subprocess.Popen[bytes],
        timeout: float | None = None,
        guard: subprocess.Popen[bytes] | None = None,
    ) -> None:
        limit = float(
            timeout if timeout is not None else self.settings.linux_runtime_start_timeout_seconds
        )
        deadline = time.monotonic() + limit
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                raise RuntimeError("服务正在停止，放弃等待 Chrome CDP")
            if process.poll() is not None:
                raise RuntimeError(f"Chrome 提前退出，退出码 {process.returncode}")
            if guard is not None and guard.poll() is not None:
                raise RuntimeError(f"Xvfb 提前退出，退出码 {guard.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    return
            except OSError as exc:
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"等待 Chrome CDP 端口 {port} 超时：{last_error}")

    # ------------------------------------------------------------------
    # 启动 / 重启
    # ------------------------------------------------------------------

    def _resolve_binaries(self) -> None:
        xvfb = self._find_binary("Xvfb")
        chrome = self._find_binary(
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        )
        if not xvfb or not chrome:
            missing = [
                name
                for name, value in (("Xvfb", xvfb), ("Chrome/Chromium", chrome))
                if not value
            ]
            raise MissingBrowserRuntime(
                "Linux 无桌面自动浏览器缺少："
                + "、".join(missing)
                + "。Ubuntu/Debian 可安装 xvfb、google-chrome-stable（或 chromium）。"
            )
        self._binaries = {"xvfb": xvfb, "chrome": chrome}

    def _start_display(self) -> None:
        display = self.settings.linux_display
        os.environ["DISPLAY"] = display
        if self._x_server_ready():
            # 复用外部已有的 X 服务（裸机场景），本进程不负责它的生命周期。
            self._owns_display = False
            self._note(f"复用已存在的 X 服务 {display}")
            return
        self._clear_stale_x_locks()
        try:
            X11_SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        xvfb = self._spawn(
            [
                self._binaries["xvfb"],
                display,
                "-screen",
                "0",
                "1440x940x24",
                # 最后一个客户端断开时不重置服务，Chromium 重启后可直接复用。
                "-noreset",
                "-nolisten",
                "tcp",
            ]
        )
        self._xvfb = xvfb
        self._owns_display = True
        try:
            self._wait_for_x_server(xvfb, self.settings.linux_runtime_start_timeout_seconds)
        except RuntimeError as exc:
            self._terminate(xvfb)
            self._forget(xvfb)
            self._xvfb = None
            self._owns_display = False
            raise self._with_log_tail(exc) from exc

    def _start_chrome(self) -> None:
        self._clear_stale_chromium_locks()
        chrome = self._spawn(self._build_chrome_command(self._binaries["chrome"]))
        self._chrome = chrome
        try:
            self._wait_for_port(
                self.settings.linux_cdp_port,
                chrome,
                guard=self._xvfb if self._owns_display else None,
            )
        except RuntimeError as exc:
            self._terminate(chrome)
            self._forget(chrome)
            self._chrome = None
            raise self._with_log_tail(exc) from exc
        self.generation += 1

    def _with_log_tail(self, exc: Exception) -> RuntimeError:
        tail = self._log_tail()
        detail = f"\nChromium/Xvfb 日志尾部：\n{tail}" if tail else ""
        return RuntimeError(f"{exc}{detail}")

    def start(self) -> None:
        self.settings.ensure_dirs()
        self._open_log()
        self._resolve_binaries()
        self.settings.browser_cdp_url = f"http://127.0.0.1:{self.settings.linux_cdp_port}"
        with self._lock:
            self._start_display()
            self._start_chrome()
            self._start_novnc()
        self.last_error = None
        self.start_supervisor()

    @property
    def ready(self) -> bool:
        # 刻意不加锁：/api/health 在事件循环里调用，恢复流程持锁时不能把它堵住。
        chrome = self._chrome
        if chrome is None or chrome.poll() is not None:
            return False
        if self._owns_display:
            xvfb = self._xvfb
            if xvfb is None or xvfb.poll() is not None:
                return False
        return True

    def status(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "generation": self.generation,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
        }

    def degrade(self, exc: BaseException) -> None:
        """启动失败时进入降级模式：Web 管理页照常可用，后台按退避重试拉起浏览器。"""
        self.last_error = str(exc)
        if self.log_file is None:
            try:
                self._open_log()
            except OSError:
                pass
        self._note(f"浏览器运行时启动失败，转入后台重试：{exc}")
        self.settings.browser_cdp_url = f"http://127.0.0.1:{self.settings.linux_cdp_port}"
        self.start_supervisor()

    # ------------------------------------------------------------------
    # 看门狗
    # ------------------------------------------------------------------

    def start_supervisor(self) -> None:
        if not self.settings.linux_runtime_supervise:
            return
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._stopping.clear()
        self._supervisor = threading.Thread(
            target=self._supervise,
            name="linux-runtime-supervisor",
            daemon=True,
        )
        self._supervisor.start()

    def _supervise(self) -> None:
        interval = max(1, int(self.settings.linux_runtime_supervise_seconds))
        backoff = float(interval)
        max_backoff = float(max(interval, self.settings.linux_runtime_max_backoff_seconds))
        while not self._stopping.wait(interval):
            self._rotate_log_if_oversized()
            if self.ready:
                backoff = float(interval)
                continue
            if self._stopping.is_set():
                return
            try:
                self._recover()
            except Exception as exc:  # 看门狗自身绝不能因为异常而退出
                self.restart_count += 1
                self.last_error = str(exc)
                self._note(f"浏览器运行时恢复失败（第 {self.restart_count} 次）：{exc}")
                if self._stopping.wait(backoff):
                    return
                backoff = min(max_backoff, backoff * 2)
            else:
                backoff = float(interval)

    def _recover(self) -> None:
        with self._lock:
            if self._stopping.is_set():
                return
            if not self._binaries:
                self._resolve_binaries()
            chrome_dead = self._chrome is None or self._chrome.poll() is not None
            display_dead = self._owns_display and (
                self._xvfb is None or self._xvfb.poll() is not None
            )
            if not chrome_dead and not display_dead:
                return
            self.restart_count += 1
            self._note(
                f"检测到浏览器运行时已退出（Xvfb={'dead' if display_dead else 'ok'}，"
                f"Chrome={'dead' if chrome_dead else 'ok'}），开始第 {self.restart_count} 次恢复"
            )
            self._terminate(self._chrome)
            self._forget(self._chrome)
            self._chrome = None
            if display_dead or self._xvfb is None:
                self._terminate(self._xvfb)
                self._forget(self._xvfb)
                self._xvfb = None
                self._owns_display = False
                self._start_display()
            self._start_chrome()
            self._restart_novnc_if_needed()
            self.last_error = None
            self._note(f"浏览器运行时已恢复，generation={self.generation}")

    # ------------------------------------------------------------------
    # noVNC
    # ------------------------------------------------------------------

    def _start_novnc(self) -> None:
        if not self.settings.linux_novnc_enabled:
            return
        x11vnc = self._find_binary("x11vnc")
        websockify = self._find_binary("websockify")
        if not x11vnc or not websockify or not self.settings.linux_novnc_web_dir.is_dir():
            raise MissingBrowserRuntime(
                "已启用 noVNC，但缺少 x11vnc、websockify 或 noVNC Web 目录。"
                "Ubuntu/Debian 请安装：x11vnc novnc websockify。"
            )
        display = self.settings.linux_display
        vnc_command = [
            x11vnc,
            "-display",
            display,
            "-localhost",
            "-forever",
            "-shared",
            "-nap",
            "-wait",
            str(max(10, self.settings.linux_vnc_poll_ms)),
            "-defer",
            str(max(10, self.settings.linux_vnc_defer_ms)),
            "-rfbport",
            str(self.settings.linux_vnc_port),
        ]
        password = self.settings.linux_vnc_password
        bind_address = self.settings.linux_novnc_bind_address
        if password:
            password_file = self.settings.data_dir / "vnc.pass"
            result = subprocess.run(
                [x11vnc, "-storepasswd", password, str(password_file)],
                stdin=subprocess.DEVNULL,
                stdout=self.log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("生成 noVNC 密码文件失败")
            password_file.chmod(0o600)
            vnc_command.extend(["-rfbauth", str(password_file)])
        elif bind_address not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("noVNC 监听非本机地址时必须设置 DOUYIN_LINUX_VNC_PASSWORD")
        else:
            vnc_command.append("-nopw")
        self._vnc_command = vnc_command
        self._websockify_command = [
            websockify,
            f"--web={self.settings.linux_novnc_web_dir}",
            f"{bind_address}:{self.settings.linux_novnc_port}",
            f"127.0.0.1:{self.settings.linux_vnc_port}",
        ]
        self._vnc = self._spawn(vnc_command)
        self._websockify = self._spawn(self._websockify_command)

    def _restart_novnc_if_needed(self) -> None:
        """X 服务重建后 x11vnc 会失去连接，需要跟着重启。"""
        if self._vnc_command is None or self._websockify_command is None:
            return
        for process in (self._vnc, self._websockify):
            self._terminate(process)
            self._forget(process)
        self._vnc = self._spawn(self._vnc_command)
        self._websockify = self._spawn(self._websockify_command)

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stopping.set()
        supervisor = self._supervisor
        if supervisor is not None and supervisor.is_alive() and supervisor is not threading.current_thread():
            supervisor.join(timeout=5)
        self._supervisor = None
        with self._lock:
            for process in reversed(self.processes):
                self._terminate(process, timeout=5)
            self.processes.clear()
            self._chrome = None
            self._xvfb = None
            if self._owns_display:
                # 主动收回锁文件，下一次启动就不会再撞上"display already active"。
                for path in (self._x_lock_path(), self._x_socket_path()):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._owns_display = False
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None


def prepare_linux_runtime(settings: Settings) -> LinuxRuntime | None:
    if not sys.platform.startswith("linux"):
        return None
    if settings.browser_cdp_url or settings.browser_headless or os.environ.get("DISPLAY"):
        return None
    if not settings.linux_auto_browser:
        raise RuntimeError(
            "当前 Linux 没有 DISPLAY。请配置 DOUYIN_BROWSER_CDP_URL，或启用 "
            "DOUYIN_LINUX_AUTO_BROWSER=true。"
        )
    runtime = LinuxRuntime(settings)
    try:
        runtime.start()
    except MissingBrowserRuntime:
        # 缺少依赖是配置问题，重试多少次都不会好，直接暴露给运维。
        runtime.stop()
        raise
    except Exception as exc:
        # 其余失败一律降级：容器保持存活，管理页可访问，后台持续重试。
        print(f"浏览器运行时启动失败，已转入后台重试：{exc}")
        runtime.degrade(exc)
    set_active_runtime(runtime)
    return runtime

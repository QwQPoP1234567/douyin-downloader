from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

from app.config import Settings


class LinuxRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.processes: list[subprocess.Popen[bytes]] = []
        self.log_file: IO[bytes] | None = None

    @staticmethod
    def _find_binary(*names: str) -> str | None:
        return next((path for name in names if (path := shutil.which(name))), None)

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
    def _wait_for_port(port: int, process: subprocess.Popen[bytes], timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        first_attempt = True
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Chrome 提前退出，退出码 {process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    return
            except OSError as exc:
                last_error = exc
                # First retry is immediate; later checks only yield briefly for Chrome startup.
                if first_attempt:
                    first_attempt = False
                    continue
                time.sleep(0.05)
        raise RuntimeError(f"等待 Chrome CDP 端口 {port} 超时：{last_error}")

    def start(self) -> None:
        self.settings.ensure_dirs()
        self.log_file = (self.settings.data_dir / "linux-runtime.log").open("ab", buffering=0)
        xvfb = self._find_binary("Xvfb")
        chrome = self._find_binary(
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        )
        if not xvfb or not chrome:
            missing = [name for name, value in (("Xvfb", xvfb), ("Chrome/Chromium", chrome)) if not value]
            raise RuntimeError(
                "Linux 无桌面自动浏览器缺少："
                + "、".join(missing)
                + "。Ubuntu/Debian 可安装 xvfb、google-chrome-stable（或 chromium）。"
            )

        display = self.settings.linux_display
        self._spawn(
            [
                xvfb,
                display,
                "-screen",
                "0",
                "1440x940x24",
                "-nolisten",
                "tcp",
            ]
        )
        os.environ["DISPLAY"] = display
        self._clear_stale_chromium_locks()
        chrome_command = [
            chrome,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.settings.linux_cdp_port}",
            f"--user-data-dir={self.settings.browser_data_dir}",
            "--disable-dev-shm-usage",
            "--disable-notifications",
            "--keep-alive-for-test",
            "--password-store=basic",
            "--no-first-run",
            "--window-size=1440,940",
            "https://www.douyin.com/",
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            chrome_command.insert(1, "--no-sandbox")
        chrome_process = self._spawn(chrome_command)
        try:
            self._wait_for_port(self.settings.linux_cdp_port, chrome_process)
        except RuntimeError as exc:
            tail = self._log_tail()
            detail = f"\nChromium/Xvfb 日志尾部：\n{tail}" if tail else ""
            raise RuntimeError(f"{exc}{detail}") from exc
        self.settings.browser_cdp_url = (
            f"http://127.0.0.1:{self.settings.linux_cdp_port}"
        )

        if self.settings.linux_novnc_enabled:
            x11vnc = self._find_binary("x11vnc")
            websockify = self._find_binary("websockify")
            if not x11vnc or not websockify or not self.settings.linux_novnc_web_dir.is_dir():
                raise RuntimeError(
                    "已启用 noVNC，但缺少 x11vnc、websockify 或 noVNC Web 目录。"
                    "Ubuntu/Debian 请安装：x11vnc novnc websockify。"
                )
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
                raise RuntimeError(
                    "noVNC 监听非本机地址时必须设置 DOUYIN_LINUX_VNC_PASSWORD"
                )
            else:
                vnc_command.append("-nopw")
            self._spawn(vnc_command)
            self._spawn(
                [
                    websockify,
                    f"--web={self.settings.linux_novnc_web_dir}",
                    f"{bind_address}:{self.settings.linux_novnc_port}",
                    f"127.0.0.1:{self.settings.linux_vnc_port}",
                ]
            )

    def stop(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5
        for process in reversed(self.processes):
            if process.poll() is not None:
                continue
            timeout = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
        self.processes.clear()
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
    except Exception:
        runtime.stop()
        raise
    return runtime

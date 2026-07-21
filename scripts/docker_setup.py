from __future__ import annotations

import html
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


WORKSPACE = Path(os.environ.get("SETUP_WORKSPACE", "/workspace")).resolve()
TOKEN = os.environ.get("SETUP_TOKEN") or secrets.token_urlsafe(24)
PORT = int(os.environ.get("SETUP_PORT", "8780"))


DEFAULTS = {
    "mysql_database": "douyin",
    "mysql_user": "douyin",
    "data_path": "./data",
    "download_path": "./downloads",
    "browser_path": "./browser_data",
    "web_port": "8765",
    "novnc_port": "6080",
    "download_concurrency": "1",
    "novnc_enabled": "true",
    "mysql_buffer_pool": "128M",
}


def generated_secret() -> str:
    return secrets.token_urlsafe(32)


def env_value(value: str) -> str:
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise ValueError("配置值不能包含换行")
    if not value:
        return ""
    if any(character.isspace() for character in value) or "#" in value or '"' in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        values[key.strip()] = value
    return values


def build_env(form: dict[str, str], existing: dict[str, str] | None = None) -> str:
    existing = existing or {}
    def pick(field: str, env_key: str, default: str) -> str:
        return form.get(field) or existing.get(env_key) or default

    web_port = int(form.get("web_port", "8765"))
    novnc_port = int(form.get("novnc_port", "6080"))
    concurrency = int(form.get("download_concurrency", "1"))
    if not 1 <= web_port <= 65535 or not 1 <= novnc_port <= 65535:
        raise ValueError("端口必须在 1～65535 之间")
    if not 1 <= concurrency <= 3:
        raise ValueError("下载并发必须在 1～3 之间")

    values = {
        "TZ": "Asia/Shanghai",
        "MYSQL_DATABASE": pick("mysql_database", "MYSQL_DATABASE", "douyin"),
        "MYSQL_USER": pick("mysql_user", "MYSQL_USER", "douyin"),
        "MYSQL_PASSWORD": pick("mysql_password", "MYSQL_PASSWORD", generated_secret()),
        "MYSQL_ROOT_PASSWORD": pick("mysql_root_password", "MYSQL_ROOT_PASSWORD", generated_secret()),
        "MYSQL_INNODB_BUFFER_POOL_SIZE": pick("mysql_buffer_pool", "MYSQL_INNODB_BUFFER_POOL_SIZE", "128M"),
        "MYSQL_INNODB_LOG_BUFFER_SIZE": "16M",
        "MYSQL_MAX_CONNECTIONS": "30",
        "DOUYIN_DATABASE_POOL_SIZE": "3",
        "DOUYIN_DATABASE_MAX_OVERFLOW": "1",
        "DOUYIN_DATABASE_POOL_RECYCLE_SECONDS": "1800",
        "DOUYIN_DATABASE_CONNECT_RETRIES": "3",
        "DOUYIN_MAX_SCAN_SCROLLS": "300",
        "DOUYIN_SCAN_MAX_RUNTIME_SECONDS": "900",
        "DOUYIN_SCAN_BATCH_SIZE": "30",
        "DOUYIN_SCAN_NO_PROGRESS_SECONDS": "90",
        "DOUYIN_SCAN_CONTINUE_LIMIT": "100",
        "DOUYIN_SCHEDULE_JITTER_SECONDS": "120",
        "DOUYIN_PREVIEW_SESSION_TTL_MINUTES": "120",
        "DOUYIN_WEB_PORT": str(web_port),
        "DOUYIN_NOVNC_PORT": str(novnc_port),
        "DOUYIN_VNC_PASSWORD": pick("vnc_password", "DOUYIN_VNC_PASSWORD", generated_secret()),
        "DOUYIN_DATA_PATH": pick("data_path", "DOUYIN_DATA_PATH", "./data"),
        "DOUYIN_DOWNLOAD_PATH": pick("download_path", "DOUYIN_DOWNLOAD_PATH", "./downloads"),
        "DOUYIN_BROWSER_PATH": pick("browser_path", "DOUYIN_BROWSER_PATH", "./browser_data"),
        "DOUYIN_DOWNLOAD_CONCURRENCY": str(concurrency),
        "DOUYIN_LINUX_NOVNC_ENABLED": "true" if form.get("novnc_enabled") == "true" else "false",
        "DOUYIN_LINUX_NOVNC_MODE": "on_demand",
        "DOUYIN_LINUX_NOVNC_IDLE_SECONDS": "120",
        "DOUYIN_DINGTALK_ENABLED": "false",
        "DOUYIN_DINGTALK_WEBHOOK": "",
        "DOUYIN_DINGTALK_SECRET": "",
    }
    return "# 由 Docker 初始化向导生成。请妥善保存，勿提交到 Git。\n" + "\n".join(
        f"{key}={env_value(value)}" for key, value in values.items()
    ) + "\n"


def page(content: str, *, title: str = "Docker 初始化向导") -> bytes:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>
    :root{{color-scheme:dark;font-family:Inter,'Microsoft YaHei',system-ui,sans-serif;background:#0e1014;color:#f5f7fa}}*{{box-sizing:border-box}}body{{margin:0;padding:32px 16px;background:radial-gradient(circle at 10% 0%,#28202e,#0e1014 38%)}}main{{width:min(920px,100%);margin:auto}}.hero,.card{{background:#171a20;border:1px solid #2a2f39;border-radius:18px;padding:24px;margin-bottom:18px}}h1{{margin:0 0 8px;font-size:32px}}h2{{margin:0 0 16px}}p,small{{color:#9aa2ae;line-height:1.6}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}label{{display:grid;gap:7px;color:#cbd0d8}}input,select{{width:100%;min-height:44px;border:1px solid #3b424e;border-radius:10px;background:#101319;color:#fff;padding:10px 12px}}.wide{{grid-column:1/-1}}.check{{display:flex;align-items:center}}.check input{{width:auto;min-height:auto}}button{{border:0;border-radius:11px;background:#ff3158;color:#fff;padding:13px 20px;font-weight:800;cursor:pointer}}.actions{{display:flex;justify-content:flex-end;margin-top:20px}}.note{{padding:12px;border-radius:10px;background:#101319;color:#9aa2ae}}code{{display:block;padding:14px;border-radius:10px;background:#0b0d11;color:#28e2cf;overflow:auto}}.error{{color:#ff8ca1}}@media(max-width:680px){{body{{padding:14px 10px}}.grid{{grid-template-columns:1fr}}.wide{{grid-column:auto}}}}
    </style></head><body><main>{content}</main></body></html>""".encode("utf-8")


def form_page(message: str = "") -> bytes:
    existing = read_env(WORKSPACE / ".env")
    fields = {
        "data_path": existing.get("DOUYIN_DATA_PATH", DEFAULTS["data_path"]),
        "download_path": existing.get("DOUYIN_DOWNLOAD_PATH", DEFAULTS["download_path"]),
        "browser_path": existing.get("DOUYIN_BROWSER_PATH", DEFAULTS["browser_path"]),
        "web_port": existing.get("DOUYIN_WEB_PORT", DEFAULTS["web_port"]),
        "novnc_port": existing.get("DOUYIN_NOVNC_PORT", DEFAULTS["novnc_port"]),
        "mysql_database": existing.get("MYSQL_DATABASE", DEFAULTS["mysql_database"]),
        "mysql_user": existing.get("MYSQL_USER", DEFAULTS["mysql_user"]),
        "download_concurrency": existing.get("DOUYIN_DOWNLOAD_CONCURRENCY", DEFAULTS["download_concurrency"]),
        "novnc_enabled": existing.get("DOUYIN_LINUX_NOVNC_ENABLED", DEFAULTS["novnc_enabled"]).lower(),
        "mysql_buffer_pool": existing.get("MYSQL_INNODB_BUFFER_POOL_SIZE", DEFAULTS["mysql_buffer_pool"]),
    }
    fields = {key: html.escape(value, quote=True) for key, value in fields.items()}
    selected = lambda actual, expected: " selected" if actual == expected else ""
    notice = f'<p class="error">{html.escape(message)}</p>' if message else ""
    return page(f"""<section class="hero"><h1>抖音下载器 Docker 初始化</h1><p>只需设置存储目录和端口。数据库、root 与 noVNC 密码留空时会自动生成高强度随机值。</p>{notice}</section>
    <form class="card" method="post" action="/save?token={html.escape(TOKEN)}"><h2>存储与访问</h2><div class="grid">
    <label class="wide">应用数据目录<input name="data_path" value="{fields['data_path']}" required></label>
    <label class="wide">作品下载目录<input name="download_path" value="{fields['download_path']}" required></label>
    <label class="wide">浏览器会话目录<input name="browser_path" value="{fields['browser_path']}" required></label>
    <label>管理页面端口<input name="web_port" type="number" min="1" max="65535" value="{fields['web_port']}" required></label>
    <label>noVNC 端口<input name="novnc_port" type="number" min="1" max="65535" value="{fields['novnc_port']}" required></label>
    <label>下载并发<select name="download_concurrency"><option value="1"{selected(fields['download_concurrency'], '1')}>1（低性能 NAS 推荐）</option><option value="2"{selected(fields['download_concurrency'], '2')}>2</option><option value="3"{selected(fields['download_concurrency'], '3')}>3</option></select></label>
    <label>noVNC<select name="novnc_enabled"><option value="true"{selected(fields['novnc_enabled'], 'true')}>启用</option><option value="false"{selected(fields['novnc_enabled'], 'false')}>关闭</option></select></label></div>
    <h2 style="margin-top:24px">数据库与安全</h2><div class="grid"><label>数据库名称<input name="mysql_database" value="{fields['mysql_database']}" required></label><label>数据库用户<input name="mysql_user" value="{fields['mysql_user']}" required></label><label>MySQL 密码（留空则首次自动生成，升级时保留原值）<input name="mysql_password" type="password"></label><label>MySQL root 密码（留空则首次自动生成，升级时保留原值）<input name="mysql_root_password" type="password"></label><label>noVNC 密码（留空则首次自动生成，升级时保留原值）<input name="vnc_password" type="password"></label><label>MySQL 缓冲池<select name="mysql_buffer_pool"><option value="128M"{selected(fields['mysql_buffer_pool'], '128M')}>128M（低内存推荐）</option><option value="256M"{selected(fields['mysql_buffer_pool'], '256M')}>256M</option><option value="512M"{selected(fields['mysql_buffer_pool'], '512M')}>512M</option></select></label><label class="wide check"><input name="overwrite" type="checkbox" value="true"> 允许覆盖已有 .env</label></div><p class="note">密码只写入本机项目目录的 .env，不会显示在完成页面；覆盖配置时留空会保留现有密码。</p><div class="actions"><button type="submit">生成 Docker 配置</button></div></form>""")


class Handler(BaseHTTPRequestHandler):
    def authorized(self) -> bool:
        return parse_qs(urlparse(self.path).query).get("token", [""])[0] == TOKEN

    def send_html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self.authorized():
            self.send_html(page("<section class='card'><h1>访问令牌无效</h1><p>请使用容器日志中输出的完整初始化地址。</p></section>"), 403)
            return
        self.send_html(form_page())

    def do_POST(self) -> None:
        if not self.authorized() or urlparse(self.path).path != "/save":
            self.send_html(page("<section class='card'><h1>无权执行</h1></section>"), 403)
            return
        length = min(int(self.headers.get("Content-Length", "0")), 65536)
        parsed = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        form = {key: values[-1] for key, values in parsed.items()}
        target = WORKSPACE / ".env"
        try:
            if target.exists() and form.get("overwrite") != "true":
                raise ValueError(".env 已存在；如需替换，请勾选允许覆盖。")
            content = build_env(form, read_env(target))
            temp = WORKSPACE / ".env.tmp"
            temp.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temp, target)
            target.chmod(0o600)
        except (OSError, ValueError) as exc:
            self.send_html(form_page(str(exc)), 400)
            return
        self.send_html(page("""<section class="hero"><h1>配置已生成</h1><p>.env 已安全写入项目目录。现在关闭初始化向导，并启动正式服务。</p></section><section class="card"><h2>下一步</h2><code>docker compose -f docker-compose.setup.yml down<br>docker compose up -d --build</code><p>启动后访问管理页面；首次使用请打开 noVNC 完成抖音扫码登录。</p></section>""", title="配置已生成"))

    def log_message(self, format: str, *args: object) -> None:
        try:
            print(f"[setup] {self.address_string()} - {format % args}", flush=True)
        except OSError:
            pass


def main() -> None:
    print("\nDocker 初始化向导已启动。", flush=True)
    print(f"请打开：http://<NAS-IP>:{PORT}/?token={TOKEN}\n", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse

import uvicorn

from app.config import settings
from app.linux_runtime import prepare_linux_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动抖音视频订阅下载器")
    parser.add_argument("--host", default=settings.host, help="监听地址，默认仅本机访问")
    parser.add_argument("--port", type=int, default=settings.port, help="管理网页端口")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    linux_runtime = prepare_linux_runtime(settings)
    if linux_runtime:
        print(
            "Linux 虚拟桌面已启动："
            f"CDP=127.0.0.1:{settings.linux_cdp_port}，"
            f"noVNC={settings.linux_novnc_bind_address}:{settings.linux_novnc_port}"
        )
    print(f"管理页面启动地址：http://{args.host}:{args.port}")
    try:
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            reload=False,
            log_level="info",
        )
    finally:
        if linux_runtime:
            linux_runtime.stop()


if __name__ == "__main__":
    main()

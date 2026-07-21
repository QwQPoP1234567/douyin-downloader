# 抖音订阅下载器

[![Tests](https://github.com/QwQPoP1234567/douyin-downloader/actions/workflows/tests.yml/badge.svg)](https://github.com/QwQPoP1234567/douyin-downloader/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个本地优先的抖音作品订阅、归档和播放工具。通过真实 Chromium 会话读取当前账号有权访问的作品信息，支持定时扫描、预览选择、下载队列、图文归档、本地封面缓存和沉浸式播放器。

> 本项目不会绕过验证码、权限控制、付费限制或平台安全机制。只能保存当前账号有权查看的内容。请遵守平台规则、作者权利和适用法律。

## 功能

- 监控多个作者，支持分钟、小时、每天和多天调度；
- 首次预览最近作品，支持后端分页、筛选和跨页选择；
- 多种下载策略：手动选择、自动下载新作、只保存信息、进入待确认；
- 扫描任务和下载任务持久化，支持暂停、继续、取消和失败重试；
- SQLite 本地运行，Docker 默认使用 MySQL 8.4；
- 视频与图文统一作品库，按作者分组并显示本地文件状态；
- 本地视频 Range 播放、图文轮播、倍速、缩放、键盘和滚轮切换；
- 封面优先缓存到本地，降低远程地址失效和风控影响；
- 钉钉通知、运行日志、健康检查和 SQLite → MySQL 迁移工具；
- Linux Docker、低性能 NAS、群晖和 Windows 本地运行支持。

## Docker 快速开始

首次部署不需要手工填写大量 `.env`。在项目目录运行初始化向导：

```bash
docker compose -f docker-compose.setup.yml up
```

日志会输出带一次性令牌的地址：

```text
http://<服务器IP>:8780/?token=<一次性令牌>
```

通过网页设置存储目录、端口、下载并发和 noVNC。MySQL、root 和 noVNC 密码留空会自动生成。

生成配置后执行：

```bash
docker compose -f docker-compose.setup.yml down
docker compose up -d --build
```

默认地址：

- 管理页面：`http://<服务器IP>:8765`
- noVNC：`http://<服务器IP>:6080/vnc.html`
- 健康检查：`http://<服务器IP>:8765/api/health`

详细步骤见 [Linux Docker 部署教程](docs/LINUX_DOCKER_GUIDE.md)。

## Windows 本地运行

需要 Python 3.12+ 和 Chrome：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
python main.py
```

访问 <http://127.0.0.1:8765>。未设置 `DATABASE_URL` 时默认使用 `data/douyin.db`；登录会话保存在 `browser_data/`，下载文件保存在 `downloads/`。这些运行数据均已通过 `.gitignore` 排除。

## 首次使用

1. 打开管理页面；
2. 点击“打开登录 / 验证浏览器”；
3. 在真实 Chromium 或 noVNC 页面中扫码登录；
4. 粘贴作者主页链接并获取作品；
5. 在预览页选择作品、调度和下载策略；
6. 确认后在作品库、下载任务和日志页面查看状态。

遇到滑块、扫码、短信或其他安全验证时，需要在浏览器中手动完成。程序不会自动绕过验证。

## 数据与隐私

以下目录可能包含账号、Cookie、作者信息或下载内容，禁止提交到 Git：

- `.env`
- `data/`
- `browser_data/`
- `downloads/`
- `logs/`
- `.playwright-cli/`

公开日志或 Issue 前请移除 Token、Cookie、Webhook、作者 UID、作品链接和本机绝对路径。详见 [安全政策](SECURITY.md)。

## 文档

- [文档首页](docs/README.md)
- [Linux Docker 部署教程](docs/LINUX_DOCKER_GUIDE.md)
- [完整部署手册](docs/DEPLOYMENT.md)
- [SQLite → MySQL 迁移](docs/SQLITE_TO_MYSQL.md)
- [运行维护与故障排查](docs/OPERATIONS.md)
- [架构与 API Wiki](docs/wiki/Home.md)

## 开发

```bash
python -m pip install -r requirements.txt
python -m pytest -q
node --check app/static/app.js
```

贡献代码前请阅读 [贡献指南](CONTRIBUTING.md) 和 [社区行为准则](CODE_OF_CONDUCT.md)。

## 安全提示

- 管理页面默认只应在本机或可信局域网使用；
- 不要将 Chrome CDP `9222`、VNC `5900`、noVNC `6080` 或初始化向导 `8780` 直接暴露到公网；
- 初始化完成后关闭 `douyin-setup` 容器；
- 定期备份 `.env`、MySQL、浏览器会话和下载目录；
- 发现漏洞请使用 GitHub 私密漏洞报告，不要在公开 Issue 中粘贴敏感信息。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方组件及许可证信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

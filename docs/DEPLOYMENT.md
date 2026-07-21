# 部署手册

本文覆盖 Windows、原生 Linux、Docker 和群晖 Container Manager。程序统一从根目录的 `main.py` 启动，不依赖 PowerShell 或 Shell 启动脚本。

## 1. 部署前确认

推荐环境：

- Python 3.12；当前代码也在 Python 3.13 上通过测试。
- Chrome、Chromium 或 Playwright Chromium。
- 至少 1 GB 可用内存；Docker 建议给共享内存保留 1 GB。
- 能访问抖音和实际媒体 CDN 的网络。
- 持久化保存 `data`、`browser_data` 和 `downloads`。

首次登录、验证码和安全验证必须由用户在真实浏览器界面中完成。程序不会绕过验证码，也不能访问当前登录账号无权查看的内容。

## 2. 持久目录

| 目录 | 内容 | 是否必须备份 |
| --- | --- | --- |
| `data/` | SQLite 数据库、Linux 浏览器日志、VNC 密码文件 | 是 |
| `browser_data/` | Chrome Profile、Cookie 和登录状态 | 是 |
| `downloads/` | 视频、图文原图、封面和 `metadata.jsonl` | 是 |

镜像和源代码都不包含这些运行数据。复制 `browser_data` 前必须停止程序或容器，避免复制到一半时 Cookie 数据库仍在写入。

## 3. Windows 部署

### 3.1 安装

在 PowerShell 中进入项目目录：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

默认优先使用系统 Chrome；安装 Playwright Chromium 是回退方案。

### 3.2 启动

```powershell
python main.py
```

打开 <http://127.0.0.1:8765>，点击“打开登录 / 验证浏览器”并扫码。默认运行目录为项目根目录，数据库和下载文件会写入本项目的持久目录。

修改管理端口：

```powershell
python main.py --port 9000
```

### 3.3 Windows 更新

停止旧进程后更新代码，再执行：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
python main.py
```

不要删除 `browser_data`，否则需要重新登录。

## 4. 原生 Linux 部署

### 4.1 Debian/Ubuntu 依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv xvfb x11vnc novnc websockify chromium
```

不同发行版的软件包管理器和 Chromium 包名可能不同。程序实际要求以下命令至少能找到对应实现：

```text
Xvfb
google-chrome / google-chrome-stable / chromium / chromium-browser
x11vnc                 # 启用 noVNC 时需要
websockify             # 启用 noVNC 时需要
/usr/share/novnc       # 默认 noVNC Web 文件目录
```

### 4.2 Python 环境

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### 4.3 无桌面运行方式

当 Linux 没有 `DISPLAY` 且没有配置外部 CDP 时，`main.py` 自动启动：

```text
Xvfb → Chrome/Chromium → CDP 9222
                    └→ x11vnc → websockify/noVNC 6080
```

启动：

```bash
python main.py
```

默认只监听本机。远程使用推荐 SSH 隧道：

```bash
ssh -L 8765:127.0.0.1:8765 -L 6080:127.0.0.1:6080 user@server
```

然后在自己的电脑访问：

- 管理页：<http://127.0.0.1:8765>
- noVNC：<http://127.0.0.1:6080/vnc.html>

不要把 Chrome CDP `9222` 或原始 VNC `5900` 暴露到公网。

### 4.4 systemd

复制并修改示例：

```bash
sudo cp deploy/douyin.service.example /etc/systemd/system/douyin.service
sudo editor /etc/systemd/system/douyin.service
sudo systemctl daemon-reload
sudo systemctl enable --now douyin
sudo systemctl status douyin
```

示例默认使用 `/opt/douyin` 和用户 `douyin`，必须按实际路径和用户修改。

查看日志：

```bash
journalctl -u douyin -n 200 --no-pager
tail -n 200 data/linux-runtime.log
```

## 5. Docker 部署

Dockerfile 基于 `python:3.12-slim-bookworm`。`apt-get` 在镜像构建容器内部执行，因此宿主机不需要提供 `apt`。

### 5.1 准备配置

```bash
cp .env.docker.example .env
```

至少修改：

```dotenv
DOUYIN_VNC_PASSWORD=替换为强密码
DOUYIN_DATA_PATH=./data
DOUYIN_DOWNLOAD_PATH=./downloads
DOUYIN_BROWSER_PATH=./browser_data
```

群晖应改成 `/volume1/docker/douyin/...` 形式的绝对路径。

### 5.2 构建和启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 douyin-downloader
```

旧版 Compose：

```bash
docker-compose up -d --build
```

验证健康状态：

```bash
curl http://127.0.0.1:8765/api/health
```

预期包含：

```json
{"ok":true,"scheduler_running":true,"browser_cdp_ok":true}
```

### 5.3 Docker 更新

代码或 Dockerfile 更新后：

```bash
docker compose down
docker compose up -d --build
docker compose ps
```

普通重启不需要重新构建：

```bash
docker compose restart douyin-downloader
```

不要执行 `docker compose down -v`，本项目使用绑定挂载时通常不会删除宿主目录，但养成不带 `-v` 的操作习惯更安全。

## 6. 群晖 Container Manager

### 6.1 群晖能否直接构建

可以。DSM 没有 `apt` 不影响，因为 Dockerfile 的 `apt-get` 在 Debian 构建层中运行。群晖需要：

- 已安装 Container Manager 或 Docker 套件；
- 能访问基础镜像仓库和 Debian 软件源；
- 有足够内存、磁盘和构建时间；
- 基础镜像与 Chromium 支持群晖 CPU 架构。

查看架构：

```bash
uname -m
```

常见结果：

- `x86_64`：通常可以直接构建和运行。
- `aarch64`：需确认所用镜像和 Debian Chromium 的 ARM64 支持。
- `armv7l`：当前浏览器运行栈通常不适合直接部署。

如果源码已经上传到群晖，可使用带 `build:` 的标准 Compose：

```bash
docker compose up -d --build
```

### 6.2 推荐方式：外部构建后导入

在与群晖相同 CPU 架构的 Linux 上构建并完成基本测试：

```bash
docker compose up -d --build
docker compose ps
docker save -o douyin-subscription-downloader.tar douyin-subscription-downloader:local
```

复制 tar 到群晖后：

```bash
docker load -i douyin-subscription-downloader.tar
docker image ls | grep douyin-subscription-downloader
```

`docker-compose.synology.yml` 只引用已导入镜像，不包含 `build:`：

```bash
cp .env.docker.example .env
```

编辑 `.env`：

```dotenv
DOUYIN_DATA_PATH=/volume1/docker/douyin/data
DOUYIN_DOWNLOAD_PATH=/volume1/docker/douyin/downloads
DOUYIN_BROWSER_PATH=/volume1/docker/douyin/browser_data
DOUYIN_WEB_PORT=8765
DOUYIN_NOVNC_PORT=6080
DOUYIN_VNC_PASSWORD=替换为强密码
```

创建目录并启动：

```bash
mkdir -p /volume1/docker/douyin/data
mkdir -p /volume1/docker/douyin/downloads
mkdir -p /volume1/docker/douyin/browser_data
docker compose -f docker-compose.synology.yml up -d
docker compose -f docker-compose.synology.yml ps
```

### 6.3 Container Manager 图形界面

1. 在“映像”中导入 `douyin-subscription-downloader.tar`。
2. 在“项目”中创建项目并上传 `docker-compose.synology.yml`。
3. 将 `.env` 放到项目 YAML 同一目录，或在界面中配置等价环境变量。
4. 确认三个宿主目录映射到 `/app/data`、`/app/downloads`、`/app/browser_data`。
5. 启动后检查容器健康状态和日志。

### 6.4 群晖访问安全

当前 WebUI 没有内置账号密码。只应在可信局域网、VPN、SSH 隧道或带认证的反向代理后访问。不要直接把 8765 和 6080 暴露到互联网。

## 7. 迁移、备份与恢复

### 7.1 备份

先停止容器：

```bash
docker compose down
```

再备份 `.env` 中明确配置的三个宿主目录。不要在 `down` 之后再通过 `docker inspect` 查询挂载路径，因为容器已经不存在。

示例：

```bash
tar -czf douyin-state.tar.gz data browser_data downloads
```

群晖绝对路径示例：

```bash
tar -czf /volume1/docker/douyin-state.tar.gz -C /volume1/docker douyin
```

### 7.2 恢复

1. 停止目标容器。
2. 将三个目录恢复到 Compose 配置的宿主路径。
3. 确认 Docker 对目录有读写权限。
4. 启动容器并查看日志。

```bash
docker compose up -d
docker compose logs --tail=200 douyin-downloader
```

### 7.3 只迁移登录状态

可只复制 `browser_data`，但必须停止源端和目标端浏览器。跨操作系统复制 Chrome Profile 不保证所有加密 Cookie 都可读取；Linux 到 Linux、同类容器到同类容器成功率最高。若状态无法复用，保留数据库和下载目录，重新扫码登录即可。

## 8. noVNC 与资源占用

- `DOUYIN_LINUX_NOVNC_ENABLED=false`：不启动 x11vnc 和 websockify；Chrome、Xvfb、扫描和下载继续运行。
- `DOUYIN_LINUX_VNC_POLL_MS`、`DOUYIN_LINUX_VNC_DEFER_MS`：控制 VNC 轮询与延迟。
- `DOUYIN_LINUX_NOVNC_MODE`、`DOUYIN_LINUX_NOVNC_IDLE_SECONDS`：当前是预留配置，尚未动态启停 noVNC。

关闭 noVNC 能减少屏幕抓取和 WebSocket 转发开销，但不会停止 Chrome 自身的页面渲染。程序会关闭完成任务的扫描页、详情页和推荐页，并保留一个空白锚点页防止 Chrome 因“最后一个标签页被关闭”而退出。

## 9. 部署后检查清单

- 管理页可访问。
- `/api/health` 返回 `ok=true`。
- 容器或服务重启后登录状态仍在。
- `data/douyin.db` 持续存在。
- 下载文件落入预期宿主目录。
- noVNC 已设置强密码或只通过 SSH 隧道访问。
- 添加测试用户后能完成扫描、下载和 SQLite 去重。
- 钉钉启用时测试通知发送成功。


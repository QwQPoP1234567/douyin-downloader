# 运行维护与故障排查

## 1. 日常检查

Docker：

```bash
docker compose ps
docker compose logs --tail=100 douyin-downloader
curl http://127.0.0.1:8765/api/health
```

原生 Linux：

```bash
systemctl status douyin
journalctl -u douyin -n 100 --no-pager
tail -n 100 data/linux-runtime.log
```

Windows：打开管理页并查看“运行日志”，或在启动 PowerShell 中查看 Uvicorn 输出。

## 2. 健康状态含义

`GET /api/health` 检查：

- SQLite 是否可读写；
- APScheduler 是否运行；
- 配置外部 CDP 时，CDP TCP 端口是否可连接。

Docker 状态从 `health: starting` 变成 `healthy` 可能需要几十秒，因为镜像给浏览器启动预留了 45 秒。

## 3. 常见问题

### 3.1 `Target.createTarget: Failed to open a new tab`

旧版本的无用页面清理会误关 persistent context 的最后一个页面，使 Chrome 退出但留下暂时失效的 `BrowserContext`。当前版本通过保留一个 `about:blank` 锚点页修复。

更新后必须重启 Python 进程；Docker 必须重新构建镜像：

```bash
docker compose down
docker compose up -d --build
```

不需要删除 `browser_data`。

### 3.2 容器反复重启

先不要反复执行删除 Profile 的命令，先查看：

```bash
docker compose logs --tail=200 douyin-downloader
```

若主日志只显示 Linux 浏览器启动失败，再查看绑定目录中的：

```bash
tail -n 200 data/linux-runtime.log
```

常见原因：

- 宿主挂载目录无写权限；
- Chromium、Xvfb、x11vnc 或 noVNC 文件缺失；
- 6080、8765 或内部 CDP 端口冲突；
- `browser_data` 在另一容器或 Chrome 进程中使用；
- VNC 对外监听但没有配置密码。

### 3.3 `SingletonLock` 或 Profile 正在使用

Linux 自动运行时只会清理以下进程级临时文件，不会删除 Cookie：

```text
SingletonCookie
SingletonLock
SingletonSocket
DevToolsActivePort
```

只有确认没有任何 Chrome/容器正在使用该 Profile 时才可手动清理。不要删除整个 `browser_data`。

### 3.4 `docker inspect` 提示容器不存在

执行 `docker compose down` 后，容器对象已经删除，无法再 inspect。挂载位置应直接从 `.env` 读取：

```bash
grep '^DOUYIN_.*_PATH=' .env
```

在停止容器前查询也可以：

```bash
docker inspect douyin-downloader --format '{{json .Mounts}}'
```

### 3.5 主页作品获取不全

程序不会仅凭 DOM 滚动停止判断扫描完整。完整扫描需要主页接口明确返回 `has_more=0`，并结合主页显示数量进行判断。

检查运行日志中的：

- 主页显示数量与实际捕获数量；
- `has_more` 是否一直为 1；
- 是否出现验证码、风控响应或网络超时；
- 是否排除了非目标作者的推荐作品；
- 是否包含图文和日常内容。

不完整扫描不会用于判断作品删除或私密，避免误标记。

### 3.6 验证码

出现验证码后：

1. 任务进入 `needs_verification`。
2. 自动调度跳过该用户，避免持续撞风控。
3. 打开登录/验证浏览器或 noVNC。
4. 手工完成验证。
5. 回到管理页点击“立即扫描”。

程序不会自动识别、破解或跳过验证码。

### 3.7 noVNC CPU 较高

首次登录完成后可关闭 noVNC：

```dotenv
DOUYIN_LINUX_NOVNC_ENABLED=false
```

然后重建/重启容器。需要处理验证码时重新启用。当前 `on_demand` 和空闲秒数只是预留项，不会自动关闭 x11vnc/noVNC。

### 3.8 下载失败或媒体地址过期

下载器会：

- 对临时网络错误立即重试；
- 使用 `.part` 和 `.part.url` 断点续传；
- CDN 地址失效时重新打开作品页捕获真实媒体地址后再试；
- 保留失败次数和详细错误；
- 启用钉钉时发送下载失败/持续失败通知。

可以在管理页对单条失败记录点击重试。

### 3.9 已删除或私密内容

连续两次完整扫描均未发现某作品后，`remote_status` 才变为 `removed_or_private`。本地已下载文件不会删除，管理页显示“已删除或私密但已下载”。

### 3.10 管理页下载目录无法修改

Docker Compose 设置了 `DOUYIN_DOWNLOAD_DIR=/app/downloads`，因此 WebUI 会锁定下载目录。请修改宿主挂载：

```dotenv
DOUYIN_DOWNLOAD_PATH=/新的宿主目录
```

然后重建容器。

## 4. 备份策略

建议：

- `data/`：每天备份。
- `browser_data/`：每次确认登录正常后备份；备份前停止容器。
- `downloads/`：按数据量使用快照、增量备份或群晖 Hyper Backup。
- `.env`：安全保存，但不要提交到 Git。

SQLite 使用 WAL 模式。在线复制时必须同时处理 `douyin.db`、`douyin.db-wal` 和 `douyin.db-shm`；更简单可靠的方法是停止程序后复制整个 `data` 目录。

## 5. 安全注意事项

- 当前 WebUI 没有内置鉴权。
- 不向公网暴露 9222/CDP 和 5900/VNC。
- noVNC 对非本机地址监听时必须设置强密码。
- 8765/6080 只放在可信局域网、VPN、SSH 隧道或带认证的反向代理之后。
- 钉钉 Webhook 和 `SEC...` 密钥保存在 SQLite；备份文件也应当作敏感数据保护。


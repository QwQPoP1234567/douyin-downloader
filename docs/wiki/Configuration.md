# 配置参考

配置由 `pydantic-settings` 读取。变量统一使用 `DOUYIN_` 前缀；本机运行时可在项目根目录创建 `.env`。

## 1. 应用与目录

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_APP_NAME` | `抖音视频订阅下载器` | 页面和 API 标题 |
| `DOUYIN_HOST` | `127.0.0.1` | Web 监听地址 |
| `DOUYIN_PORT` | `8765` | Web 端口 |
| `DOUYIN_DATA_DIR` | `./data` | SQLite 和运行日志目录 |
| `DOUYIN_DOWNLOAD_DIR` | `./downloads` | 下载目录；设置后 WebUI 不允许修改 |
| `DOUYIN_BROWSER_DATA_DIR` | `./browser_data` | Chrome 持久 Profile |

## 2. 浏览器

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_BROWSER_HEADLESS` | `false` | 无头模式；不适合需要人工验证码的场景 |
| `DOUYIN_BROWSER_CHANNEL` | `chrome` | Playwright 浏览器 channel；为空时使用 bundled Chromium |
| `DOUYIN_BROWSER_PROXY` | 空 | 浏览器代理，如 `http://127.0.0.1:10808` |
| `DOUYIN_BROWSER_CDP_URL` | 空 | 外部 Chrome CDP，如 `http://127.0.0.1:9222` |

配置外部 CDP 后，应用不再启动自己的 persistent context，并且关闭应用时只断开 Playwright，不关闭外部 Chrome。

## 3. Linux 运行环境

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_LINUX_AUTO_BROWSER` | `true` | 无 `DISPLAY` 时自动启动 Linux 浏览器栈 |
| `DOUYIN_LINUX_DISPLAY` | `:99` | Xvfb Display |
| `DOUYIN_LINUX_CDP_PORT` | `9222` | Chrome CDP 端口，仅监听本机 |
| `DOUYIN_LINUX_NOVNC_ENABLED` | `true` | 是否启动 x11vnc 和 noVNC |
| `DOUYIN_LINUX_NOVNC_MODE` | `on_demand` | 预留项，当前未实现动态启停 |
| `DOUYIN_LINUX_NOVNC_IDLE_SECONDS` | `120` | 预留项，当前未使用 |
| `DOUYIN_LINUX_VNC_PORT` | `5900` | 原始 VNC 端口，仅监听本机 |
| `DOUYIN_LINUX_NOVNC_PORT` | `6080` | noVNC WebSocket 端口 |
| `DOUYIN_LINUX_NOVNC_BIND_ADDRESS` | `127.0.0.1` | noVNC 监听地址 |
| `DOUYIN_LINUX_NOVNC_WEB_DIR` | `/usr/share/novnc` | noVNC 静态文件目录 |
| `DOUYIN_LINUX_VNC_PASSWORD` | 空 | 非本机监听时必填 |
| `DOUYIN_LINUX_VNC_POLL_MS` | `80` | x11vnc 屏幕轮询间隔 |
| `DOUYIN_LINUX_VNC_DEFER_MS` | `80` | x11vnc 更新延迟 |

## 4. 扫描与下载

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_DEFAULT_INTERVAL_MINUTES` | `60` | 新用户默认检查间隔 |
| `DOUYIN_SCAN_POLL_SECONDS` | `30` | 调度器检查到期用户的频率 |
| `DOUYIN_SCAN_SCROLL_WAIT_MS` | `1300` | 每次滚动后等待时间基数 |
| `DOUYIN_SCAN_STABLE_ROUNDS` | `7` | DOM/分页稳定判断轮数 |
| `DOUYIN_MAX_SCAN_SCROLLS` | `1000` | 单次扫描最大滚动次数 |
| `DOUYIN_SCHEDULE_JITTER_RATIO` | `0.1` | 扫描间隔随机抖动，代码限制 0 到 0.5 |
| `DOUYIN_DOWNLOAD_CONCURRENCY` | `2` | 下载并发，代码限制 1 到 8 |
| `DOUYIN_REQUEST_TIMEOUT_SECONDS` | `90` | HTTP 下载超时 |

过短的扫描间隔会提高验证码和风控概率。API 对单用户间隔限制为 5 到 10080 分钟。

## 5. 钉钉

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_DINGTALK_ENABLED` | `false` | 是否启用通知 |
| `DOUYIN_DINGTALK_WEBHOOK` | 空 | 钉钉官方机器人 HTTPS Webhook |
| `DOUYIN_DINGTALK_SECRET` | 空 | `SEC...` 加签密钥 |

WebUI 中保存的钉钉设置写入 SQLite，并优先于环境变量。API 只返回脱敏 Webhook，不返回 secret。

## 6. Docker Compose 专用变量

以下变量主要用于 Compose 插值，不是 `Settings` 字段：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 容器时区 |
| `DOUYIN_DATA_PATH` | `./data` | 宿主数据目录 |
| `DOUYIN_DOWNLOAD_PATH` | `./downloads` | 宿主下载目录 |
| `DOUYIN_BROWSER_PATH` | `./browser_data` | 宿主 Profile 目录 |
| `DOUYIN_WEB_PORT` | `8765` | 宿主管理页端口 |
| `DOUYIN_NOVNC_PORT` | `6080` | 宿主 noVNC 端口 |
| `DOUYIN_VNC_PASSWORD` | 必填 | Compose 传给容器内 VNC 密码配置 |

注意：Compose 的 `.env` 首先用于 YAML 插值。只有 `docker-compose.yml` 的 `environment:` 中列出的值才会传入容器。若要在 Docker 中增加新的 `DOUYIN_*` 设置，需要同时更新 Compose 的 `environment:`，或显式增加 `env_file`。


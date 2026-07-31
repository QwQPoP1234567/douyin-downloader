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

### 安全验证保持

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_VERIFICATION_RECHECK_SECONDS` | `3600` | 撞上验证码后自动复查的间隔，60～86400 秒 |

抖音要求安全验证时，程序会停止**全部**自动扫描与下载，并且不再新开任何页面 —— 否则每个到期的主播都会各自开一个标签页再撞一次，Chromium 很快堆满验证页，人也无从操作。

页面保持在原处等待人工完成。恢复方式有两种：在管理页点“我已完成验证”立即复查，或者等待每 `DOUYIN_VERIFICATION_RECHECK_SECONDS` 一次的自动复查。复查只检查已经打开的页面，不会新开标签页。

保持状态写在数据库里，重启进程后依然有效。程序不会尝试自动破解或绕过验证码。

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
| `DOUYIN_LINUX_RUNTIME_START_TIMEOUT_SECONDS` | `60` | 等待 X 服务与 Chrome CDP 就绪的超时，NAS 磁盘慢时可调大 |
| `DOUYIN_LINUX_RUNTIME_SUPERVISE` | `true` | 后台看门狗：Xvfb/Chromium 退出后自动拉起 |
| `DOUYIN_LINUX_RUNTIME_SUPERVISE_SECONDS` | `10` | 看门狗检查间隔 |
| `DOUYIN_LINUX_RUNTIME_MAX_BACKOFF_SECONDS` | `300` | 反复恢复失败时的退避上限 |
| `DOUYIN_LINUX_RUNTIME_LOG_MAX_BYTES` | `8388608` | `data/linux-runtime.log` 超过该大小就地截断，0 表示不限制 |
| `DOUYIN_LINUX_CHROMIUM_DISK_CACHE_MB` | `128` | Chromium 磁盘/媒体缓存上限，防止 `browser_data` 无限增长 |

## 4. 扫描与下载

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_DEFAULT_INTERVAL_MINUTES` | `60` | 新用户默认检查间隔 |
| `DOUYIN_SCAN_POLL_SECONDS` | `30` | 调度器检查到期用户的频率 |
| `DOUYIN_SCAN_CONCURRENCY` | `1` | 同时进行的后台扫描数上限，代码限制 1 到 3。浏览器只有一把扫描页锁，调大只会让任务堆在锁上一起超时；到期但派发不出去的用户会留在队列里，由下一轮轮询接手 |
| `DOUYIN_SCAN_SCROLL_WAIT_MS` | `1300` | 每次滚动后等待时间基数 |
| `DOUYIN_SCAN_STABLE_ROUNDS` | `7` | DOM/分页稳定判断轮数 |
| `DOUYIN_MAX_SCAN_SCROLLS` | `1000` | 单次扫描最大滚动次数 |
| `DOUYIN_SCHEDULE_JITTER_RATIO` | `0.1` | 扫描间隔随机抖动，代码限制 0 到 0.5 |
| `DOUYIN_DOWNLOAD_CONCURRENCY` | `2` | 下载并发，代码限制 1 到 8 |
| `DOUYIN_REQUEST_TIMEOUT_SECONDS` | `90` | HTTP 下载超时 |

过短的扫描间隔会提高验证码和风控概率。API 对单用户间隔限制为 5 到 10080 分钟。

每次重新排程时，抖动取 `[0, jitter_seconds]` 内的随机值而不是固定偏移，否则同一批创建的用户会永远同时到期；容器崩溃恢复后，所有过期用户也会由 `DOUYIN_SCAN_CONCURRENCY` 逐批消化，不会一起涌向浏览器。

## 5. 历史数据保留

事件日志和扫描任务记录随运行时长无上限增长，7×24 跑几个月会撑爆数据库卷并拖慢管理页列表。调度器按固定周期回收：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_RETENTION_SWEEP_HOURS` | `6` | 回收任务的执行间隔 |
| `DOUYIN_LOG_RETENTION_DAYS` | `30` | 事件日志保留天数，0 表示不按时间清理 |
| `DOUYIN_LOG_MAX_ROWS` | `200000` | 事件日志行数上限兜底，0 表示不限制 |
| `DOUYIN_SCAN_JOB_RETENTION_DAYS` | `30` | 已结束（完成/失败/取消）扫描任务的保留天数，0 表示不清理 |

`download_jobs` 受 `video_id` 唯一约束限制，行数有界且是重复下载判定的依据，不参与回收。

## 6. 钉钉

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUYIN_DINGTALK_ENABLED` | `false` | 是否启用通知 |
| `DOUYIN_DINGTALK_WEBHOOK` | 空 | 钉钉官方机器人 HTTPS Webhook |
| `DOUYIN_DINGTALK_SECRET` | 空 | `SEC...` 加签密钥 |

WebUI 中保存的钉钉设置写入 SQLite，并优先于环境变量。API 只返回脱敏 Webhook，不返回 secret。

## 7. Docker Compose 专用变量

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
| `DOUYIN_TMPFS_SIZE` | `256m` | 容器 `/tmp` 的 tmpfs 大小。`/tmp` 必须是 tmpfs，否则 X11 锁会跨重启存活并让 Xvfb 误判 display 被占用 |
| `DOUYIN_LOG_MAX_SIZE` | `10m` | 单个容器日志文件上限 |
| `DOUYIN_LOG_MAX_FILE` | `3` | 容器日志文件保留个数 |

注意：Compose 的 `.env` 首先用于 YAML 插值。只有 `docker-compose.yml` 的 `environment:` 中列出的值才会传入容器。若要在 Docker 中增加新的 `DOUYIN_*` 设置，需要同时更新 Compose 的 `environment:`，或显式增加 `env_file`。


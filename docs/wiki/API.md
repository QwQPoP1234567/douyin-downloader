# HTTP API

默认基础地址：`http://127.0.0.1:8765`。当前 API 没有鉴权，只能部署在可信网络或认证代理之后。

FastAPI 自动文档通常可通过 `/docs` 访问。

## 1. 健康与状态

### `GET /api/health`

返回数据库、调度器和 CDP 健康状态。

### `GET /api/status`

返回浏览器、登录、验证码、下载目录和任务数量。登录判定优先使用成功的个人资料接口响应，其次检查关键登录 Cookie。

## 2. 登录浏览器

### `POST /api/login/open`

打开或复用登录/验证码页面。

### `POST /api/login/close`

关闭应用管理的浏览器 context；CDP 模式只断开连接，不关闭外部 Chrome。

## 3. 监控用户

### `GET /api/creators`

列出全部监控用户。

### `POST /api/creators`

请求：

```json
{
  "profile_url": "https://www.douyin.com/user/SEC_UID",
  "interval_minutes": 60
}
```

添加成功后立即启动首次扫描。间隔范围为 5 到 10080 分钟。

### `PATCH /api/creators/{creator_id}`

可修改：

```json
{
  "enabled": true,
  "interval_minutes": 120
}
```

### `DELETE /api/creators/{creator_id}`

删除数据库中的监控用户及级联作品记录。本地下载文件保留。扫描运行时返回 409。

### `POST /api/creators/{creator_id}/scan`

人工立即扫描。若任务已经运行，返回 `started=false`。

## 4. 作品

### `GET /api/videos`

查询参数：

- `creator_id`：可选。
- `status`：可选。
- `limit`：1 到 2000，默认 200。

### `POST /api/videos/{video_id}/retry`

把作品改回 `pending`，并启动所属创作者扫描任务。

## 5. 日志

### `GET /api/logs?limit=200`

返回最近事件，limit 范围为 1 到 1000。

## 6. 设置

### `GET /api/settings`

返回下载目录、是否被环境变量锁定和默认扫描间隔。

### `PATCH /api/settings`

```json
{
  "download_dir": "D:\\DouyinDownloads"
}
```

设置 `DOUYIN_DOWNLOAD_DIR` 时返回 409，Docker 中应修改挂载而不是容器内路径。

## 7. 钉钉

### `GET /api/notifications/dingtalk`

只返回启用状态、是否配置完整和脱敏 Webhook。

### `PATCH /api/notifications/dingtalk`

```json
{
  "enabled": true,
  "webhook": "https://oapi.dingtalk.com/robot/send?access_token=...",
  "secret": "SEC..."
}
```

只接受钉钉官方 HTTPS 域名和带 `access_token` 的地址。

### `POST /api/notifications/dingtalk/test`

发送测试 Markdown 消息。发送失败返回 502，未启用返回 400。

## 8. 错误约定

- 400：参数、URL、目录或通知配置无效。
- 404：创作者或作品不存在。
- 409：重复用户、任务冲突或环境变量锁定设置。
- 500：浏览器启动等服务器内部错误。
- 502：外部钉钉调用失败。
- 503：健康检查发现 CDP 不可用。


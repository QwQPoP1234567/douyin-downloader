# 开发指南

## 1. 本地环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m pytest -q
python main.py
```

Linux 将激活命令替换为：

```bash
. .venv/bin/activate
```

## 2. 修改前先确认边界

- `browser_data`、`data`、`downloads` 是用户运行数据，不应提交或在测试中删除。
- 工作区可能有用户未提交修改，避免覆盖无关文件。
- 不把验证码破解、自动绕过登录或越权访问作为功能实现。
- 页面结构不稳定，优先解析真实 JSON 响应，DOM 只做有限兜底。

## 3. 常见开发入口

### 新增扫描字段

1. 在 `parse_aweme()` 归一化字段。
2. 在 `Database.upsert_video()` 保存字段。
3. 必要时更新建表 SQL和旧库迁移。
4. 更新前端展示和 API 文档。
5. 增加嵌套响应、字段缺失和作者过滤测试。

### 修改主页完整性判断

必须保留以下原则：

- `has_more=true` 时不能仅因 DOM 不再变化就判定完成。
- 不完整扫描不能执行删除/私密对账。
- 推荐卡片不能混入目标作者作品。
- 图文、视频和日常都在扫描范围。

### 修改浏览器页面清理

必须保留：

- managed 页面不关闭；
- 验证码和登录页面保留；
- 至少一个空白锚点页；
- 外部 CDP 模式不关闭外部 Chrome；
- 关闭任务页时移除监听和后台 response task。

### 修改下载器

必须考虑：

- 断点文件和 URL 是否匹配；
- HTTP 206、416 和 Range 行为；
- 地址过期后重新解析；
- 视频和图文的不同目录结构；
- 已有有效文件不重复下载；
- 临时文件不能被误判为成功；
- 不选择明显水印地址。

### 新增通知事件

统一从 `SubscriptionService._notify()` 发送，避免通知异常打断主任务。详情字段应包含用户、作品 ID/链接、错误、失败次数和处理建议。

## 4. 测试

运行全部测试：

```bash
python -m pytest -q
```

当前测试覆盖：

- SQLite 去重、迁移、删除/私密确认和中断恢复；
- 主页作品解析、作者过滤和风控响应；
- 视频/图文无水印候选源选择；
- Linux 自动浏览器条件和锁文件清理；
- 钉钉签名与配置脱敏；
- 验证码暂停调度；
- persistent context 空白锚点页。

涉及真实浏览器的变更还应执行手工烟雾测试：

1. 启动服务。
2. 打开登录页。
3. 确认关闭推荐/扫描页后 Chrome 不退出。
4. 扫描一个测试主页。
5. 重启服务，确认登录和数据库状态保留。

## 5. 调试建议

Playwright 浏览器启动日志：

```powershell
$env:DEBUG='pw:browser'
python main.py
```

Linux 浏览器栈日志：

```bash
tail -f data/linux-runtime.log
```

检查 API：

```bash
curl http://127.0.0.1:8765/api/status
curl http://127.0.0.1:8765/api/logs?limit=50
```

检查 SQLite：

```bash
sqlite3 data/douyin.db '.tables'
sqlite3 data/douyin.db 'select id,nickname,status,last_error from creators;'
```

## 6. 发布与升级检查

1. `python -m pytest -q` 全部通过。
2. `docker compose config` 能正常解析。
3. Docker 镜像能构建并达到 healthy。
4. Windows persistent context 与 Linux CDP 两种模式至少各验证一次。
5. 使用旧数据库启动，确认迁移幂等。
6. 容器重启后 `browser_data`、数据库和下载文件仍在。
7. 更新部署文档、配置表和 API 文档。

## 7. 后续优先事项

- 实现 noVNC 真正按需启动和空闲停止。
- 为 WebUI 增加可选鉴权和反向代理部署示例。
- 增强浏览器/CDP 异常退出后的自动重建。
- 增加扫描接口响应样本回归测试。
- 增加数据库一致性检查和可选备份命令。
- 评估浏览器维护会话、HTTP Worker 扫描的混合模式，但保留人工验证码边界。


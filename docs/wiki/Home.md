# 程序 Wiki

本 Wiki 面向维护者和后续开发者，描述当前实现，而不是理想化设计。

## 页面索引

- [系统架构](Architecture.md)
- [配置参考](Configuration.md)
- [数据模型与状态](Data-and-State.md)
- [HTTP API](API.md)
- [开发指南](Development.md)
- [部署手册](../DEPLOYMENT.md)
- [运行维护与故障排查](../OPERATIONS.md)

## 当前能力

- 使用持久化 Chrome Profile 保存人工登录状态。
- Windows/macOS 直接由 Playwright 启动 persistent context。
- 无桌面 Linux 自动托管 Xvfb、Chrome、CDP、x11vnc 和 noVNC。
- 监听抖音主页真实 JSON 响应并滚动加载全部作品。
- 按目标作者 `sec_uid` 过滤推荐页和其他作者内容。
- 扫描视频、图文和日常，SQLite 按 `aweme_id` 去重。
- 选择无常见平台水印的播放源或原图并支持断点续传。
- 连续两次完整扫描确认远端删除/私密，本地文件保留。
- 钉钉 Webhook 加签通知验证码、成功、失败和持续错误。

## 设计边界

- 不破解或绕过验证码。
- 不获取当前账号无权查看的作品。
- 不保证移除作者烙入源画面的 Logo、字幕或昵称。
- 平台接口和页面结构变化时，扫描解析逻辑需要维护。
- WebUI 当前没有内置认证，不适合直接暴露到公网。


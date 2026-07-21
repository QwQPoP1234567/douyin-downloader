# 贡献指南

感谢你愿意改进本项目。提交代码前请先阅读以下约定。

## 开发环境

```bash
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Linux：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 提交修改

1. 从最新 `main` 创建功能分支。
2. 一个提交只处理一个清晰的小功能或修复。
3. 不要提交 `.env`、数据库、Cookie、浏览器配置、下载文件、日志或真实账号链接。
4. 新功能应补充相应测试和文档。
5. 保持 Windows 本地运行与 Linux Docker 运行的公共业务逻辑一致。

提交前运行：

```bash
python -m pytest -q
python -m compileall -q app scripts
```

前端 JavaScript 修改还应运行：

```bash
node --check app/static/app.js
```

Docker 配置修改应运行：

```bash
docker compose config --quiet
docker compose -f docker-compose.setup.yml config --quiet
```

## Pull Request

PR 描述请说明：

- 修改了什么；
- 为什么需要修改；
- 对用户和已有数据的影响；
- 执行过哪些验证；
- 是否涉及数据库迁移、下载目录或浏览器会话。

## 使用边界

本项目不会绕过验证码、权限控制或付费限制。请勿提交用于规避平台安全机制、批量滥用账号或侵犯作者权益的改动。

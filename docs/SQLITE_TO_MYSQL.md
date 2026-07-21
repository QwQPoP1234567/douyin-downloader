# SQLite → MySQL 迁移与备份

本手册适用于把旧版 `data/douyin.db` 一次性迁移到 MySQL 8。迁移工具会复制监控用户、作品、下载状态和文件路径、运行日志、应用设置及钉钉配置；本地视频、封面和浏览器登录目录需要单独复制。

## 1. 迁移前准备

1. 停止旧应用，避免迁移期间继续写入 SQLite。
2. 备份 `.env`、`data/`、`browser_data/` 和 `downloads/`。
3. 确认目标 MySQL 为空库，并使用 `utf8mb4` 字符集。
4. 先只启动 MySQL，确认健康检查通过，不要同时启动新应用。

Docker Compose 示例：

```bash
docker compose up -d mysql
docker compose ps mysql
```

迁移脚本默认拒绝写入非空目标库，以避免把两套数据意外混合。

## 2. 执行迁移

在项目根目录安装当前依赖后运行：

```bash
export DATABASE_URL='mysql+pymysql://douyin:你的密码@127.0.0.1:3306/douyin?charset=utf8mb4'
python scripts/migrate_sqlite_to_mysql.py data/douyin.db
```

如果 MySQL 只暴露在 Compose 网络内，可在应用镜像中执行同一脚本，并把旧 SQLite 文件只读挂载进去。数据库地址使用服务名 `mysql`：

```text
mysql+pymysql://douyin:密码@mysql:3306/douyin?charset=utf8mb4
```

脚本开始前使用 SQLite Backup API 创建一致性备份，默认文件名类似：

```text
data/douyin.db.20260720-203000.bak
```

可显式指定备份位置：

```bash
python scripts/migrate_sqlite_to_mysql.py data/douyin.db \
  --backup-path backups/douyin-before-mysql.db
```

只有在已经核对目标库、明确需要续跑时，才使用 `--allow-nonempty`。不要把它作为常规参数。

## 3. 迁移结果

脚本以 JSON 输出以下统计：

- 每张表的 `created`、`skipped`、`failed` 数量；
- 检测到的重复用户和重复作品数量；
- 已检查、本地存在和本地缺失的文件数量；
- 源库去重后的用户/作品数量与目标库实际数量；
- SQLite 一致性备份路径；
- `validation_ok` 和最终 `success` 状态。

退出码含义：

| 退出码 | 含义 |
| --- | --- |
| `0` | 迁移完成，记录数校验通过且没有失败记录 |
| `1` | 参数、连接或执行过程失败 |
| `2` | 脚本执行完成，但记录数校验失败或存在失败记录 |

缺失的本地文件会计入 `missing_files`，不会让数据库记录消失。启动应用后页面应继续显示相应下载状态，后续可单独重试下载。

## 4. 启动新应用前核对

至少核对：

1. `validation_ok` 为 `true`。
2. `failed_total` 为 `0`。
3. 监控用户数和作品数与旧库去重后的数量一致。
4. 已下载作品的 `file_path`、`cover_path` 和下载状态仍然存在。
5. 钉钉 Webhook、secret 和启用状态已迁移。
6. `downloads/` 与 `browser_data/` 已复制到新部署使用的持久目录。

确认后再启动应用：

```bash
docker compose up -d
docker compose logs -f --tail=200 douyin-downloader
```

## 5. MySQL 日常备份与恢复

数据库备份：

```bash
mkdir -p backups
docker compose exec -T mysql sh -c \
  'exec mysqldump --single-transaction --routines --triggers -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
  > backups/douyin-$(date +%Y%m%d-%H%M%S).sql
```

恢复前先停止应用，避免恢复时产生新写入：

```bash
docker compose stop douyin-downloader
docker compose exec -T mysql sh -c \
  'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
  < backups/douyin-YYYYMMDD-HHMMSS.sql
docker compose start douyin-downloader
```

数据库备份不包含视频、封面和浏览器登录状态。应同时备份：

- MySQL 持久化卷；
- `downloads/` 持久目录；
- `browser_data/` 持久目录；
- `.env`，并按敏感文件保护。

## 6. 回滚到旧 SQLite

如新环境尚未产生必须保留的新数据，可按以下步骤回滚：

1. 停止新应用。
2. 保留 MySQL dump，避免丢失排查证据。
3. 恢复迁移前生成的 `.bak` 文件为 `data/douyin.db`。
4. 从旧部署配置中移除 `DATABASE_URL`，让兼容模式重新使用 SQLite。
5. 恢复对应版本的应用代码并启动。

如果 MySQL 已经产生新扫描或下载记录，不要直接回滚覆盖；应先导出并比对新增数据，再制定合并方案。

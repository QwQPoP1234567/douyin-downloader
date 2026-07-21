# Linux Docker 部署教程

本文适用于 Ubuntu、Debian、CentOS、Rocky Linux、OpenMediaVault 以及支持 Docker Compose 的普通 Linux NAS。群晖 Container Manager 也可以参考相同目录和命令。

项目默认包含以下容器：

- `douyin-downloader`：管理页面、扫描、下载、Chromium、Xvfb 和可选 noVNC。
- `douyin-mysql`：MySQL 8.4，保存监控用户、作品、任务和日志。
- `douyin-setup`：仅初始化时使用的临时 Web 向导，用来生成 `.env`。

## 1. 安装 Docker

先确认 Docker Engine 和 Compose 插件可用：

```bash
docker --version
docker compose version
```

Ubuntu / Debian 可以使用 Docker 官方仓库安装，也可以先用系统软件源快速安装：

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
```

将当前用户加入 Docker 用户组后，需要退出并重新登录：

```bash
sudo usermod -aG docker "$USER"
```

如果不加入用户组，后续命令前加 `sudo`。

## 2. 获取项目

进入准备保存项目的目录：

```bash
git clone <项目仓库地址> douyin
cd douyin
```

如果项目是压缩包，解压后进入包含 `docker-compose.yml` 的根目录。

## 3. 启动初始化向导

首次部署不需要手工复制或填写 `.env`。执行：

```bash
docker compose -f docker-compose.setup.yml up
```

首次运行会拉取体积较小的 `python:3.12-alpine` 镜像。日志中会出现一次性地址：

```text
请打开：http://<NAS-IP>:8780/?token=随机一次性令牌
```

将 `<NAS-IP>` 替换为服务器的局域网地址，例如：

```text
http://192.168.1.20:8780/?token=日志中的令牌
```

令牌必须保留，否则向导会返回 403。初始化完成后应立即关闭向导容器。

## 4. 填写向导

向导需要确认以下内容：

| 配置 | 推荐值 | 说明 |
| --- | --- | --- |
| 应用数据目录 | `./data` | 应用运行数据和 SQLite 兼容目录 |
| 作品下载目录 | `/volume1/video/douyin` 或 `./downloads` | 视频、图文、封面和元数据 |
| 浏览器会话目录 | `./browser_data` | 持久化抖音登录状态 |
| 管理页面端口 | `8765` | Web 管理页面 |
| noVNC 端口 | `6080` | 扫码登录和验证码操作页面 |
| 下载并发 | `1` | 低性能 NAS 推荐 1，可选 1～3 |
| noVNC | 启用 | 首次登录必须启用，后续可按需关闭 |
| MySQL 缓冲池 | `128M` | 低内存 NAS 推荐值 |

MySQL 普通密码、MySQL root 密码和 noVNC 密码可以全部留空，向导会自动生成高强度随机密码并写入 `.env`。

如果 `.env` 已存在：

- 页面会预填现有目录和端口。
- 必须勾选“允许覆盖已有 `.env`”才能保存。
- 密码输入框留空会保留原密码，不会破坏已有 MySQL 数据卷。
- `.env` 会以原子替换方式写入，并在 Linux 上设置为仅当前用户可读写。

## 5. 启动正式服务

向导提示配置生成成功后，回到终端执行：

```bash
docker compose -f docker-compose.setup.yml down
docker compose up -d --build
```

查看启动状态：

```bash
docker compose ps
```

查看应用日志：

```bash
docker compose logs -f douyin-downloader
```

查看 MySQL 日志：

```bash
docker compose logs -f mysql
```

应用会等待 MySQL 健康检查通过后再启动。首次构建需要下载 Chromium、noVNC 等依赖，耗时取决于网络速度。

## 6. 打开管理页面

浏览器访问：

```text
http://<服务器IP>:8765
```

如果向导修改了管理页面端口，请使用实际端口。

健康检查地址：

```text
http://<服务器IP>:8765/api/health
```

正常时返回包含 `"ok": true` 的 JSON。

## 7. 首次扫码登录

首次部署需要通过 noVNC 操作容器内 Chromium：

```text
http://<服务器IP>:6080/vnc.html
```

使用初始化向导生成或手工填写的 noVNC 密码登录，然后：

1. 在管理页面点击“打开登录 / 验证浏览器”。
2. 在 noVNC 中找到抖音登录页面。
3. 扫码登录。
4. 如果出现滑块或验证码，在 noVNC 中手动完成。
5. 回到管理页面刷新账号状态。

浏览器会话保存在 `DOUYIN_BROWSER_PATH` 对应目录，容器更新或重建后仍会保留。

不要把 6080 端口直接暴露到公网。建议只允许局域网访问，远程使用时通过 VPN 或 SSH 隧道连接。

## 8. 防火墙

以 UFW 为例，只允许局域网网段访问：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 6080 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 8780 proto tcp
```

初始化完成并关闭向导后，可以删除 8780 的防火墙规则。

MySQL 的 3306 端口没有映射到宿主机，不需要对外开放。

## 9. 更新项目

更新前建议备份 `.env`、MySQL 数据卷、下载目录和浏览器会话目录。

```bash
git pull
docker compose up -d --build
```

容器收到停止信号后会停止领取新任务，并等待当前任务和数据库操作退出。Compose 为应用保留两分钟优雅退出时间。

如果升级需要调整目录、端口或并发，可以重新运行初始化向导：

```bash
docker compose -f docker-compose.setup.yml up
```

勾选允许覆盖，密码留空即可保留原数据库和 noVNC 密码。

## 10. 备份

### 10.1 备份 MySQL

```bash
mkdir -p backups
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot --single-transaction --routines --triggers "$MYSQL_DATABASE"' > "backups/douyin-$(date +%F-%H%M%S).sql"
```

### 10.2 备份配置和持久化目录

至少备份：

- `.env`
- `DOUYIN_DOWNLOAD_PATH` 对应目录
- `DOUYIN_BROWSER_PATH` 对应目录
- Docker 的 `mysql_data` 卷

列出 MySQL 卷：

```bash
docker volume ls | grep mysql_data
```

## 11. 从旧 SQLite 迁移

先启动 MySQL 和应用容器，并备份旧 SQLite 数据库。迁移脚本位于：

```text
scripts/migrate_sqlite_to_mysql.py
```

查看参数：

```bash
python scripts/migrate_sqlite_to_mysql.py --help
```

建议先在项目副本或测试数据库执行。脚本会统计重复作品、迁移成功/跳过数量，并检查已记录的本地文件是否存在。

## 12. 常见故障

### 12.1 `.env` 缺失或密码变量报错

重新运行初始化向导，不要手工创建空 `.env`：

```bash
docker compose -f docker-compose.setup.yml up
```

### 12.2 MySQL 一直不健康

```bash
docker compose logs --tail=200 mysql
```

检查宿主机可用内存、磁盘空间和 MySQL 数据卷权限。如果修改过已有数据库的密码，应恢复原密码；初始化向导升级时密码留空会自动保留原值。

### 12.3 应用容器无权写入下载目录

```bash
docker compose logs --tail=200 douyin-downloader
ls -ld <下载目录>
```

为兼容禁用 Chromium namespace 和特殊挂载权限的 NAS，应用容器默认以 root 运行。Docker 容器仍未获得 privileged 权限；请只挂载项目实际需要的数据、下载和浏览器目录，不要挂载宿主机根目录。

### 12.4 管理页面可打开，但显示未登录

打开 noVNC，确认 Chromium 正常运行，然后在管理页面点击“打开登录 / 验证浏览器”。完成扫码或验证码后刷新状态。

### 12.5 封面显示失败

封面会优先缓存到本地，缓存失败时临时回退远程地址。检查应用日志、下载目录写权限和抖音是否要求验证码。

### 12.6 查看容器资源占用

```bash
docker stats douyin-downloader douyin-mysql
```

低内存 NAS 建议保持下载并发为 1、MySQL 缓冲池为 128M。

## 13. 停止或卸载

停止容器但保留所有数据：

```bash
docker compose down
```

不要随意添加 `-v`，否则会删除 MySQL 数据卷。

删除项目之前，先确认 `.env`、下载目录、浏览器会话目录和 MySQL 备份已经安全保存。

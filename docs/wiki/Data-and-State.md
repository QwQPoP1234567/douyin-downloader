# 数据模型与状态

SQLite 文件默认是 `data/douyin.db`，启用 WAL 和外键。建表及兼容迁移集中在 `Database.initialize()`。

## 1. 表结构

### `creators`

保存监控用户：

- `profile_url`：主页 URL，唯一。
- `sec_uid`、`nickname`：扫描后补全。
- `enabled`、`interval_minutes`：调度配置。
- `last_scan_at`、`next_scan_at`、`latest_publish_at`：调度时间。
- `total_found`、`downloaded_count`：汇总计数。
- `status`、`last_error`：当前状态和最近错误。

### `videos`

保存作品和下载状态：

- `(creator_id, aweme_id)` 唯一去重。
- `content_type`：`video` 或 `images`。
- `asset_count`、`is_daily`：图文/日常信息。
- `video_url`、`cover_url`、`share_url`、`raw_json`：解析数据。
- `status`、`retry_count`、`last_error`：下载状态。
- `file_path`、`cover_path`、`file_size`：本地文件。
- `bytes_downloaded`、`total_bytes`：下载进度。
- `remote_status`、`missing_count`、`last_seen_at`：远端存在性。

### `event_logs`

保存页面“运行日志”所需事件，可关联创作者和作品。

### `app_settings`

保存 WebUI 修改的下载目录和钉钉配置。钉钉密钥属于敏感数据。

## 2. 创作者状态

```text
idle
  └─> scanning
        ├─> downloading ─> idle
        ├─> needs_verification
        └─> error
```

| 状态 | 含义 |
| --- | --- |
| `idle` | 等待下次扫描 |
| `scanning` | 正在扫描主页 |
| `downloading` | 正在处理待下载作品 |
| `needs_verification` | 出现验证码，自动调度暂停该用户 |
| `error` | 本次扫描失败，等待下次调度或人工重试 |

程序启动时会将上次异常退出遗留的 `scanning`、`downloading` 创作者恢复为 `idle`。

## 3. 作品下载状态

| 状态 | 含义 |
| --- | --- |
| `pending` | 等待下载或等待重新解析媒体地址 |
| `downloading` | 正在下载 |
| `downloaded` | 本地文件成功保存 |
| `failed` | 本次下载失败，可自动或手工重试 |

程序启动时把遗留的 `downloading` 作品恢复为 `pending`。

## 4. 远端状态

| 状态 | 含义 |
| --- | --- |
| `active` | 最近完整扫描仍可见 |
| `removed_or_private` | 连续两次完整扫描未出现，推断为删除或私密 |

只有完整扫描才增加 `missing_count`。不完整扫描不会改变远端状态。作品变为 `removed_or_private` 后，本地文件不删除。

## 5. 文件布局

```text
downloads/
└─ 昵称_sec_uid/
   ├─ videos/
   │  └─ awemeId_时间_文案.mp4
   ├─ covers/
   │  └─ awemeId_时间_文案.jpg
   ├─ notes/
   │  └─ awemeId_时间_文案/
   │     ├─ 001.webp
   │     └─ 002.webp
   └─ metadata.jsonl
```

文件名会清理 Windows 非法字符并限制长度。`metadata.jsonl` 每行一条已下载作品元数据；清理误收的其他作者作品后会重写。

## 6. 数据库迁移约定

当前没有 Alembic。轻量迁移在 `Database.initialize()` 中通过 `PRAGMA table_info` 和 `ALTER TABLE` 完成。增加字段时应：

1. 更新首次建表 SQL。
2. 为旧数据库增加幂等迁移。
3. 提供旧库升级测试。
4. 避免在迁移中删除本地文件或不可逆清空数据。


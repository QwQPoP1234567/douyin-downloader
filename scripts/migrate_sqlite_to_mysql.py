from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import func, select


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import Database  # noqa: E402
from app.models import AppSetting, Creator, EventLog, Video, utc_datetime  # noqa: E402


def _utc_value(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _json_object(value: Any) -> dict[str, Any] | list[Any] | None:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        decoded = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, (dict, list)) else None


@dataclass
class TableStats:
    created: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class MigrationStats:
    creators: TableStats = field(default_factory=TableStats)
    videos: TableStats = field(default_factory=TableStats)
    event_logs: TableStats = field(default_factory=TableStats)
    app_settings: TableStats = field(default_factory=TableStats)
    duplicate_creators: int = 0
    duplicate_videos: int = 0
    checked_files: int = 0
    existing_files: int = 0
    missing_files: int = 0
    expected_creators: int = 0
    actual_creators: int = 0
    expected_videos: int = 0
    actual_videos: int = 0
    backup_path: str | None = None

    @property
    def validation_ok(self) -> bool:
        return (
            self.expected_creators == self.actual_creators
            and self.expected_videos == self.actual_videos
        )

    @property
    def failed_total(self) -> int:
        return sum(
            table.failed
            for table in (self.creators, self.videos, self.event_logs, self.app_settings)
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["validation_ok"] = self.validation_ok
        result["failed_total"] = self.failed_total
        return result


class SqliteSource:
    def __init__(self, path: Path):
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def has_table(self, table: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def columns(self, table: str) -> set[str]:
        if not self.has_table(table):
            return set()
        return {str(row["name"]) for row in self.connection.execute(f"PRAGMA table_info({table})")}

    def rows(self, table: str, batch_size: int) -> Iterator[list[dict[str, Any]]]:
        if not self.has_table(table):
            return
        cursor = self.connection.execute(f"SELECT * FROM {table} ORDER BY id")
        while batch := cursor.fetchmany(batch_size):
            yield [dict(row) for row in batch]

    def duplicate_count(self, table: str, columns: tuple[str, ...]) -> int:
        available = self.columns(table)
        if not available or any(column not in available for column in columns):
            return 0
        group = ", ".join(columns)
        row = self.connection.execute(
            f"SELECT COALESCE(SUM(count_value - 1), 0) AS duplicate_count "
            f"FROM (SELECT COUNT(*) AS count_value FROM {table} GROUP BY {group} HAVING COUNT(*) > 1)"
        ).fetchone()
        return int(row["duplicate_count"] or 0)

    def unique_count(self, table: str, columns: tuple[str, ...]) -> int:
        available = self.columns(table)
        if not available or any(column not in available for column in columns):
            return 0
        group = ", ".join(columns)
        row = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM (SELECT 1 FROM {table} GROUP BY {group})"
        ).fetchone()
        return int(row["total"] or 0)


def create_consistent_backup(source_path: Path, backup_path: Path) -> Path:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


class SqliteMigrator:
    def __init__(
        self,
        source_path: Path,
        target: Database,
        *,
        batch_size: int = 200,
        allow_nonempty: bool = False,
    ):
        self.source_path = source_path
        self.source = SqliteSource(source_path)
        self.target = target
        self.batch_size = max(1, batch_size)
        self.allow_nonempty = allow_nonempty
        self.stats = MigrationStats()
        self.creator_ids: dict[int, int] = {}
        self.video_ids: dict[int, int] = {}
        self.imported_creator_ids: set[int] = set()
        self.imported_video_ids: set[int] = set()
        self.processed_video_keys: set[tuple[int, str]] = set()

    def close(self) -> None:
        self.source.close()
        self.target.close()

    def migrate(self) -> MigrationStats:
        self.target.initialize()
        self._ensure_target_is_safe()
        self.stats.duplicate_creators = self.source.duplicate_count(
            "creators", ("profile_url",)
        )
        self.stats.duplicate_videos = self.source.duplicate_count(
            "videos", ("creator_id", "aweme_id")
        )
        self.stats.expected_creators = self.source.unique_count("creators", ("profile_url",))
        self.stats.expected_videos = self.source.unique_count(
            "videos", ("creator_id", "aweme_id")
        )
        self._migrate_creators()
        self._migrate_videos()
        self._migrate_logs()
        self._migrate_settings()
        self._validate_counts()
        return self.stats

    def _ensure_target_is_safe(self) -> None:
        if self.allow_nonempty:
            return
        with self.target.session() as session:
            counts = [
                session.scalar(select(func.count()).select_from(model)) or 0
                for model in (Creator, Video, EventLog, AppSetting)
            ]
        if any(counts):
            raise RuntimeError(
                "Target database is not empty; use --allow-nonempty only for a deliberate resume"
            )

    @staticmethod
    def _value(row: dict[str, Any], key: str, default: Any = None) -> Any:
        value = row.get(key, default)
        return default if value is None else value

    def _migrate_creators(self) -> None:
        for batch in self.source.rows("creators", self.batch_size):
            with self.target.session() as session:
                for row in batch:
                    source_id = int(row["id"])
                    profile_url = str(row.get("profile_url") or "").strip()
                    if not profile_url:
                        self.stats.creators.failed += 1
                        continue
                    creator = session.scalar(
                        select(Creator).where(Creator.profile_url == profile_url)
                    )
                    if creator is None:
                        now = utc_datetime()
                        creator = Creator(
                            profile_url=profile_url,
                            sec_uid=row.get("sec_uid"),
                            nickname=row.get("nickname"),
                            avatar_url=row.get("avatar_url"),
                            enabled=_bool_value(row.get("enabled"), True),
                            status=str(self._value(row, "status", "idle")),
                            download_policy=str(
                                self._value(row, "download_policy", "metadata_only")
                            ),
                            policy_changed_at=_utc_value(row.get("policy_changed_at")),
                            per_scan_limit=int(self._value(row, "per_scan_limit", 100)),
                            interval_minutes=int(self._value(row, "interval_minutes", 60)),
                            last_scan_at=_utc_value(row.get("last_scan_at")),
                            next_scan_at=_utc_value(row.get("next_scan_at")),
                            latest_publish_at=_utc_value(row.get("latest_publish_at")),
                            total_found=int(self._value(row, "total_found", 0)),
                            downloaded_count=int(self._value(row, "downloaded_count", 0)),
                            failed_count=int(self._value(row, "failed_count", 0)),
                            pending_confirmation_count=int(
                                self._value(row, "pending_confirmation_count", 0)
                            ),
                            last_error=row.get("last_error"),
                            created_at=_utc_value(row.get("created_at")) or now,
                            updated_at=_utc_value(row.get("updated_at")) or now,
                        )
                        session.add(creator)
                        session.flush()
                        self.stats.creators.created += 1
                    else:
                        self.stats.creators.skipped += 1
                    self.creator_ids[source_id] = int(creator.id)
                    self.imported_creator_ids.add(int(creator.id))

    def _video_item(self, row: dict[str, Any]) -> dict[str, Any]:
        raw = _json_object(row.get("raw_json"))
        return {
            "aweme_id": str(row["aweme_id"]),
            "description": row.get("description"),
            "create_time": row.get("create_time"),
            "video_url": row.get("video_url"),
            "cover_url": row.get("cover_url"),
            "share_url": row.get("share_url"),
            "content_type": self._value(row, "content_type", "video"),
            "asset_count": self._value(row, "asset_count", 1),
            "is_daily": _bool_value(row.get("is_daily")),
            "raw": raw,
        }

    def _video_update(self, target_id: int, row: dict[str, Any]) -> dict[str, Any]:
        now = utc_datetime()
        return {
            "id": target_id,
            "description": row.get("description"),
            "create_time": row.get("create_time"),
            "video_url": row.get("video_url"),
            "cover_url": row.get("cover_url"),
            "share_url": row.get("share_url"),
            "content_type": str(self._value(row, "content_type", "video")),
            "asset_count": int(self._value(row, "asset_count", 1)),
            "is_daily": _bool_value(row.get("is_daily")),
            "status": str(self._value(row, "status", "pending")),
            "file_path": row.get("file_path"),
            "cover_path": row.get("cover_path"),
            "file_size": row.get("file_size"),
            "bytes_downloaded": int(self._value(row, "bytes_downloaded", 0)),
            "total_bytes": row.get("total_bytes"),
            "remote_status": str(self._value(row, "remote_status", "active")),
            "missing_count": int(self._value(row, "missing_count", 0)),
            "last_seen_at": _utc_value(row.get("last_seen_at")),
            "remote_changed_at": _utc_value(row.get("remote_changed_at")),
            "retry_count": int(self._value(row, "retry_count", 0)),
            "last_error": row.get("last_error"),
            "raw_json": row.get("raw_json"),
            "discovered_at": _utc_value(row.get("discovered_at")) or now,
            "downloaded_at": _utc_value(row.get("downloaded_at")),
            "policy_snapshot": row.get("policy_snapshot"),
            "needs_confirmation": _bool_value(row.get("needs_confirmation")),
            "created_at": _utc_value(row.get("created_at"))
            or _utc_value(row.get("discovered_at"))
            or now,
            "updated_at": _utc_value(row.get("updated_at")) or now,
        }

    def _check_file(self, value: Any) -> None:
        if not value:
            return
        path = Path(str(value))
        if not path.is_absolute():
            path = self.source_path.parent / path
        self.stats.checked_files += 1
        if path.exists():
            self.stats.existing_files += 1
        else:
            self.stats.missing_files += 1

    def _migrate_videos(self) -> None:
        for batch in self.source.rows("videos", self.batch_size):
            grouped: dict[int, list[dict[str, Any]]] = {}
            for row in batch:
                target_creator_id = self.creator_ids.get(int(row["creator_id"]))
                if target_creator_id is None or not row.get("aweme_id"):
                    self.stats.videos.failed += 1
                    continue
                grouped.setdefault(target_creator_id, []).append(row)
                self._check_file(row.get("file_path"))
                self._check_file(row.get("cover_path"))

            for creator_id, rows in grouped.items():
                results = self.target.bulk_upsert_videos(
                    creator_id, [self._video_item(row) for row in rows]
                )
                unique_aweme_ids = list(dict.fromkeys(str(row["aweme_id"]) for row in rows))
                target_by_aweme = dict(zip(unique_aweme_ids, results))
                mappings: list[dict[str, Any]] = []
                for row in rows:
                    aweme_id = str(row["aweme_id"])
                    target_id, created = target_by_aweme[aweme_id]
                    self.video_ids[int(row["id"])] = target_id
                    self.imported_video_ids.add(target_id)
                    key = (creator_id, aweme_id)
                    if key in self.processed_video_keys:
                        self.stats.videos.skipped += 1
                    elif created:
                        self.stats.videos.created += 1
                    else:
                        self.stats.videos.skipped += 1
                    self.processed_video_keys.add(key)
                    mappings.append(self._video_update(target_id, row))
                self._apply_full_video_updates(mappings)

        for creator_id in self.imported_creator_ids:
            self.target.recount_creator(creator_id)

    def _apply_full_video_updates(self, mappings: list[dict[str, Any]]) -> None:
        if not mappings:
            return
        with self.target.session() as session:
            session.bulk_update_mappings(Video, mappings)

    def _migrate_logs(self) -> None:
        for batch in self.source.rows("event_logs", self.batch_size):
            with self.target.session() as session:
                for row in batch:
                    session.add(
                        EventLog(
                            level=str(self._value(row, "level", "info")),
                            message=str(self._value(row, "message", "")),
                            creator_id=self.creator_ids.get(int(row["creator_id"]))
                            if row.get("creator_id") is not None
                            else None,
                            video_id=self.video_ids.get(int(row["video_id"]))
                            if row.get("video_id") is not None
                            else None,
                            job_type=row.get("job_type"),
                            job_id=row.get("job_id"),
                            details_json=_json_object(row.get("details_json")),
                            created_at=_utc_value(row.get("created_at")) or utc_datetime(),
                        )
                    )
                    self.stats.event_logs.created += 1

    def _migrate_settings(self) -> None:
        if not self.source.has_table("app_settings"):
            return
        order_column = "key" if "key" in self.source.columns("app_settings") else "rowid"
        cursor = self.source.connection.execute(
            f"SELECT * FROM app_settings ORDER BY {order_column}"
        )
        rows = [dict(row) for row in cursor]
        with self.target.session() as session:
            for row in rows:
                key = str(row.get("key") or "").strip()
                if not key:
                    self.stats.app_settings.failed += 1
                    continue
                setting = session.get(AppSetting, key)
                if setting is None:
                    session.add(
                        AppSetting(
                            key=key,
                            value=str(self._value(row, "value", "")),
                            updated_at=_utc_value(row.get("updated_at")) or utc_datetime(),
                        )
                    )
                    self.stats.app_settings.created += 1
                else:
                    self.stats.app_settings.skipped += 1

    def _validate_counts(self) -> None:
        self.stats.actual_creators = len(self.imported_creator_ids)
        self.stats.actual_videos = len(self.imported_video_ids)


def migrate_sqlite_database(
    source_path: Path,
    database_url: str,
    *,
    backup_path: Path | None = None,
    create_backup: bool = True,
    batch_size: int = 200,
    allow_nonempty: bool = False,
) -> MigrationStats:
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if create_backup:
        if backup_path is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = source_path.with_name(f"{source_path.name}.{stamp}.bak")
        create_consistent_backup(source_path, backup_path)
    target = Database(database_url)
    migrator = SqliteMigrator(
        source_path,
        target,
        batch_size=batch_size,
        allow_nonempty=allow_nonempty,
    )
    if backup_path is not None:
        migrator.stats.backup_path = str(backup_path.resolve())
    try:
        return migrator.migrate()
    finally:
        migrator.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate the legacy SQLite database to MySQL")
    parser.add_argument("source", type=Path, help="Path to the legacy douyin.db file")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL") or os.getenv("DOUYIN_DATABASE_URL"),
        help="MySQL SQLAlchemy URL; defaults to DATABASE_URL",
    )
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--skip-backup", action="store_true")
    parser.add_argument("--allow-nonempty", action="store_true")
    parser.add_argument("--batch-size", type=int, default=200)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if not str(args.database_url).startswith(("mysql://", "mysql+pymysql://")):
        parser.error("the command-line migration target must be MySQL")
    try:
        stats = migrate_sqlite_database(
            args.source,
            args.database_url,
            backup_path=args.backup_path,
            create_backup=not args.skip_backup,
            batch_size=args.batch_size,
            allow_nonempty=args.allow_nonempty,
        )
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    result = stats.to_dict()
    result["success"] = stats.validation_ok and stats.failed_total == 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

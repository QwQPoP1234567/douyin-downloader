import sqlite3
from pathlib import Path

from app.db import Database
from scripts.migrate_sqlite_to_mysql import migrate_sqlite_database


def create_legacy_database(path: Path, existing_file: Path, missing_file: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE creators (
            id INTEGER PRIMARY KEY,
            profile_url TEXT NOT NULL,
            sec_uid TEXT,
            nickname TEXT,
            enabled INTEGER,
            interval_minutes INTEGER,
            total_found INTEGER,
            downloaded_count INTEGER,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY,
            creator_id INTEGER NOT NULL,
            aweme_id TEXT NOT NULL,
            description TEXT,
            create_time INTEGER,
            status TEXT,
            file_path TEXT,
            cover_path TEXT,
            bytes_downloaded INTEGER,
            retry_count INTEGER,
            raw_json TEXT,
            discovered_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE event_logs (
            id INTEGER PRIMARY KEY,
            level TEXT,
            message TEXT,
            creator_id INTEGER,
            video_id INTEGER,
            created_at TEXT
        );
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        """,
    )
    connection.execute(
        "INSERT INTO creators VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            "https://www.douyin.com/user/test",
            "sec-1",
            "测试用户",
            1,
            60,
            2,
            1,
            "idle",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T01:00:00+00:00",
        ),
    )
    connection.executemany(
        "INSERT INTO videos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                10,
                1,
                "aweme-1",
                "旧标题",
                1_700_000_000,
                "downloaded",
                str(existing_file),
                None,
                123,
                0,
                '{"aweme_id":"aweme-1"}',
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T01:00:00+00:00",
            ),
            (
                11,
                1,
                "aweme-1",
                "新标题",
                1_700_000_000,
                "failed",
                str(missing_file),
                None,
                0,
                2,
                '{"aweme_id":"aweme-1"}',
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T02:00:00+00:00",
            ),
        ],
    )
    connection.execute(
        "INSERT INTO event_logs VALUES (?, ?, ?, ?, ?, ?)",
        (1, "info", "迁移日志", 1, 11, "2026-01-01T02:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO app_settings VALUES (?, ?, ?)",
        (
            "dingtalk_webhook",
            "https://example.test/hook",
            "2026-01-01T02:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()


def test_sqlite_migration_preserves_data_and_reports_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "target.db"
    backup = tmp_path / "legacy.backup.db"
    existing_file = tmp_path / "saved.mp4"
    missing_file = tmp_path / "missing.mp4"
    existing_file.write_bytes(b"video")
    create_legacy_database(source, existing_file, missing_file)

    stats = migrate_sqlite_database(
        source,
        f"sqlite:///{target.as_posix()}",
        backup_path=backup,
        batch_size=50,
    )

    assert backup.is_file()
    assert stats.validation_ok is True
    assert stats.duplicate_videos == 1
    assert stats.expected_creators == stats.actual_creators == 1
    assert stats.expected_videos == stats.actual_videos == 1
    assert stats.existing_files == 1
    assert stats.missing_files == 1
    assert stats.videos.created == 1
    assert stats.videos.skipped == 1

    db = Database(f"sqlite:///{target.as_posix()}")
    video = db.list_videos()[0]
    assert video["description"] == "新标题"
    assert video["status"] == "failed"
    assert video["retry_count"] == 2
    assert video["file_path"] == str(missing_file)
    assert db.get_setting("dingtalk_webhook") == "https://example.test/hook"
    assert db.list_logs()[0]["video_id"] == video["id"]
    db.close()

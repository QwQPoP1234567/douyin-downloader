from pathlib import Path

import sqlite3

from app.db import Database


def test_creator_and_video_are_deduplicated(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/test", 60)
    item = {
        "aweme_id": "123456789",
        "description": "第一个作品",
        "create_time": 1_700_000_000,
        "video_url": "https://example.test/video.mp4",
        "cover_url": "https://example.test/cover.jpg",
        "share_url": "https://www.douyin.com/video/123456789",
        "raw": {"aweme_id": "123456789", "video": {}},
    }

    first_id, first_created = db.upsert_video(creator["id"], item)
    second_id, second_created = db.upsert_video(creator["id"], {**item, "description": "更新文案"})

    assert first_created is True
    assert second_created is False
    assert first_id == second_id
    videos = db.list_videos(creator_id=creator["id"])
    assert len(videos) == 1
    assert videos[0]["description"] == "更新文案"


def test_bulk_upsert_videos_deduplicates_and_updates(tmp_path: Path) -> None:
    db = Database(tmp_path / "bulk-upsert.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/bulk-upsert", 60)

    first = db.bulk_upsert_videos(
        creator["id"],
        [
            {"aweme_id": "video-1", "description": "旧标题", "raw": {}},
            {"aweme_id": "video-2", "description": "第二条", "raw": {}},
            {"aweme_id": "video-1", "description": "新标题", "raw": {}},
        ],
    )
    second = db.bulk_upsert_videos(
        creator["id"],
        [{"aweme_id": "video-1", "cover_url": "https://example.com/cover.jpg"}],
    )

    assert len(first) == 2
    assert all(created for _, created in first)
    assert second == [(first[0][0], False)]
    saved = db.get_video(first[0][0])
    assert saved["description"] == "新标题"
    assert saved["cover_url"] == "https://example.com/cover.jpg"


def test_bulk_update_videos_updates_multiple_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "bulk-update.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/bulk-update", 60)
    rows = db.bulk_upsert_videos(
        creator["id"],
        [{"aweme_id": "video-1"}, {"aweme_id": "video-2"}],
    )

    count = db.bulk_update_videos(
        [
            {"id": rows[0][0], "status": "downloaded", "last_error": None},
            {"id": rows[1][0], "status": "failed", "last_error": "network"},
        ]
    )

    assert count == 2
    assert db.get_video(rows[0][0])["status"] == "downloaded"
    assert db.get_video(rows[1][0])["last_error"] == "network"
    assert db.list_video_aweme_ids(creator["id"]) == ["video-1", "video-2"]


def test_dom_stub_does_not_erase_existing_media_data(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/test", 60)
    video_id, _ = db.upsert_video(
        creator["id"],
        {
            "aweme_id": "keep-1",
            "description": "完整文案",
            "video_url": "https://cdn.test/video.mp4",
            "raw": {"video": {"play_addr": {"url_list": ["https://cdn.test/video.mp4"]}}},
        },
    )
    db.upsert_video(
        creator["id"],
        {"aweme_id": "keep-1", "description": "", "video_url": None, "raw": {}},
    )
    video = db.get_video(video_id)
    assert video["description"] == "完整文案"
    assert video["video_url"] == "https://cdn.test/video.mp4"
    assert "play_addr" in video["raw_json"]


def test_recount_creator(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/test", 60)
    video_id, _ = db.upsert_video(
        creator["id"],
        {"aweme_id": "1", "description": "", "raw": {}},
    )
    db.update_video(video_id, status="downloaded")
    db.recount_creator(creator["id"])
    updated = db.get_creator(creator["id"])
    assert updated["total_found"] == 1
    assert updated["downloaded_count"] == 1


def test_creator_list_page_is_database_paginated(tmp_path: Path) -> None:
    db = Database(tmp_path / "creator-page.db")
    db.initialize()
    for index in range(25):
        db.add_creator(f"https://www.douyin.com/user/page-{index}", 60)

    first = db.list_creators_page(page=1, page_size=20)
    second = db.list_creators_page(page=2, page_size=20)

    assert db.count_creators() == 25

    assert first["total"] == 25
    assert first["total_pages"] == 2
    assert first["page"] == 1
    assert first["page_size"] == 20
    assert len(first["items"]) == 20
    assert len(second["items"]) == 5
    assert {item["id"] for item in first["items"]}.isdisjoint(
        item["id"] for item in second["items"]
    )


def test_creator_list_page_caps_page_size_at_100(tmp_path: Path) -> None:
    db = Database(tmp_path / "creator-page-cap.db")
    db.initialize()
    db.add_creator("https://www.douyin.com/user/page-cap", 60)

    result = db.list_creators_page(page=1, page_size=500)

    assert result["page_size"] == 100


def test_video_list_page_filters_sorts_and_paginates_in_database(tmp_path: Path) -> None:
    db = Database(tmp_path / "video-page.db")
    db.initialize()
    first_creator = db.add_creator("https://www.douyin.com/user/video-page-1", 60)
    second_creator = db.add_creator("https://www.douyin.com/user/video-page-2", 60)
    db.update_creator(first_creator["id"], nickname="甲")
    db.bulk_upsert_videos(
        first_creator["id"],
        [
            {
                "aweme_id": f"match-{index}",
                "description": f"目标作品 {index}",
                "create_time": index,
                "content_type": "images" if index % 2 else "video",
            }
            for index in range(35)
        ],
    )
    db.bulk_upsert_videos(
        second_creator["id"],
        [{"aweme_id": "other", "description": "目标作品", "create_time": 999}],
    )
    failed_id = db.list_videos(creator_id=first_creator["id"])[0]["id"]
    db.update_video(failed_id, status="failed")

    first = db.list_videos_page(
        creator_id=first_creator["id"], keyword="目标", page=1, page_size=30
    )
    second = db.list_videos_page(
        creator_id=first_creator["id"], keyword="目标", page=2, page_size=30
    )
    cursor_first = db.list_videos_page(
        creator_id=first_creator["id"], keyword="目标", page_size=10
    )
    cursor_second = db.list_videos_page(
        creator_id=first_creator["id"], keyword="目标", page_size=10,
        cursor=cursor_first["next_cursor"],
    )
    images = db.list_videos_page(
        creator_id=first_creator["id"], content_type="images", sort="oldest"
    )
    failed = db.list_videos_page(creator_id=first_creator["id"], status="failed")
    exact_page = db.list_videos_page(creator_id=first_creator["id"], page_size=35)

    assert first["total"] == 35
    assert first["total_pages"] == 2
    assert len(first["items"]) == 30
    assert len(second["items"]) == 5
    assert cursor_second["pagination_mode"] == "cursor"
    assert cursor_first["items"][-1]["id"] != cursor_second["items"][0]["id"]
    assert {item["id"] for item in cursor_first["items"]}.isdisjoint(
        {item["id"] for item in cursor_second["items"]}
    )
    assert first["items"][0]["create_time"] > first["items"][-1]["create_time"]
    assert images["total"] == 17
    assert images["items"][0]["create_time"] == 1
    assert failed["total"] == 1
    assert failed["items"][0]["creator_nickname"] == "甲"
    assert exact_page["next_cursor"] is None
    assert "video_url" not in first["items"][0]


def test_video_list_page_validates_sort_and_caps_page_size(tmp_path: Path) -> None:
    db = Database(tmp_path / "video-page-validation.db")
    db.initialize()

    assert db.list_videos_page(page_size=500)["page_size"] == 100
    try:
        db.list_videos_page(sort="random")
    except ValueError as exc:
        assert "sort" in str(exc)
    else:
        raise AssertionError("invalid sort should fail")


def test_video_list_page_filters_pending_confirmation(tmp_path: Path) -> None:
    db = Database(tmp_path / "pending-confirmation-page.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/pending-page", 60)
    rows = db.bulk_upsert_videos(
        creator["id"],
        [{"aweme_id": f"pending-{index}", "create_time": index} for index in range(35)],
    )
    db.bulk_update_videos(
        [{"id": video_id, "needs_confirmation": True} for video_id, _ in rows[:31]]
    )

    first = db.list_videos_page(
        needs_confirmation=True, page=1, page_size=30
    )
    second = db.list_videos_page(
        needs_confirmation=True, page=2, page_size=30
    )

    assert first["total"] == 31
    assert first["total_pages"] == 2
    assert len(first["items"]) == 30
    assert len(second["items"]) == 1
    assert all(item["needs_confirmation"] for item in first["items"])


def test_video_lists_report_local_file_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "video-file-state.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/file-state", 60)
    rows = db.bulk_upsert_videos(
        creator["id"],
        [
            {"aweme_id": "not-downloaded"},
            {"aweme_id": "available"},
            {"aweme_id": "missing"},
        ],
    )
    existing = tmp_path / "available.mp4"
    existing.write_bytes(b"video")
    db.update_video(rows[1][0], status="downloaded", file_path=str(existing))
    db.update_video(rows[2][0], status="downloaded", file_path=str(tmp_path / "gone.mp4"))

    items = {
        item["aweme_id"]: item
        for item in db.list_videos_page(creator_id=creator["id"])["items"]
    }

    assert items["not-downloaded"]["local_file_status"] == "not_downloaded"
    assert items["not-downloaded"]["local_file_exists"] is False
    assert items["available"]["local_file_status"] == "available"
    assert items["available"]["local_file_exists"] is True
    assert items["missing"]["local_file_status"] == "missing"
    assert items["missing"]["local_file_exists"] is False


def test_video_list_treats_downloaded_image_directory_as_available(tmp_path: Path) -> None:
    db = Database(tmp_path / "image-file-state.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/image-state", 60)
    video_id, _ = db.upsert_video(
        creator["id"], {"aweme_id": "images", "content_type": "images"}
    )
    image_directory = tmp_path / "downloads" / "images"
    image_directory.mkdir(parents=True)
    db.update_video(video_id, status="downloaded", file_path=str(image_directory))

    item = db.list_videos_page(creator_id=creator["id"])["items"][0]

    assert item["local_file_status"] == "available"
    assert item["local_file_exists"] is True


def test_video_playback_context_preserves_filter_and_sort_order(tmp_path: Path) -> None:
    db = Database(tmp_path / "playback-context.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/playback", 60)
    other = db.add_creator("https://www.douyin.com/user/playback-other", 60)
    rows = db.bulk_upsert_videos(
        creator["id"],
        [
            {"aweme_id": "old", "description": "目标 old", "create_time": 100},
            {"aweme_id": "middle", "description": "目标 middle", "create_time": 200},
            {"aweme_id": "new", "description": "目标 new", "create_time": 300},
            {"aweme_id": "ignored", "description": "其他", "create_time": 400},
        ],
    )
    db.upsert_video(other["id"], {"aweme_id": "foreign", "description": "目标", "create_time": 500})
    middle_id = next(video_id for video_id, _ in rows if db.get_video(video_id)["aweme_id"] == "middle")

    newest = db.get_video_playback_context(
        middle_id, creator_id=creator["id"], keyword="目标", sort="newest"
    )
    oldest = db.get_video_playback_context(
        middle_id, creator_id=creator["id"], keyword="目标", sort="oldest"
    )

    assert newest["current"]["aweme_id"] == "middle"
    assert newest["previous"]["aweme_id"] == "new"
    assert newest["next"]["aweme_id"] == "old"
    assert newest["position"] == 2
    assert newest["total"] == 3
    assert oldest["previous"]["aweme_id"] == "old"
    assert oldest["next"]["aweme_id"] == "new"
    assert oldest["position"] == 2


def test_video_playback_context_reports_sequence_boundaries(tmp_path: Path) -> None:
    db = Database(tmp_path / "playback-boundaries.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/playback-boundaries", 60)
    old_id, _ = db.upsert_video(creator["id"], {"aweme_id": "old", "create_time": 100})
    new_id, _ = db.upsert_video(creator["id"], {"aweme_id": "new", "create_time": 200})

    first = db.get_video_playback_context(new_id, sort="newest")
    last = db.get_video_playback_context(old_id, sort="newest")

    assert first["previous"] is None
    assert first["next"]["id"] == old_id
    assert last["previous"]["id"] == new_id
    assert last["next"] is None


def test_log_list_page_filters_and_paginates(tmp_path: Path) -> None:
    db = Database(tmp_path / "log-page.db")
    db.initialize()
    for index in range(55):
        db.add_log("error" if index % 10 == 0 else "info", f"message-{index}")

    first = db.list_logs_page(page=1, page_size=50)
    second = db.list_logs_page(page=2, page_size=50)
    errors = db.list_logs_page(level="error")

    assert first["total"] == 55
    assert first["total_pages"] == 2
    assert len(first["items"]) == 50
    assert len(second["items"]) == 5
    assert first["items"][0]["message"] == "message-54"
    assert errors["total"] == 6
    assert all(item["level"] == "error" for item in errors["items"])
    assert db.list_logs_page(page_size=500)["page_size"] == 100


def test_missing_video_requires_two_complete_scans_and_keeps_local_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/test", 60)
    video_id, _ = db.upsert_video(
        creator["id"],
        {"aweme_id": "removed-1", "description": "已保存作品", "raw": {}},
    )
    local_file = tmp_path / "saved.mp4"
    local_file.write_bytes(b"video")
    db.update_video(video_id, status="downloaded", file_path=str(local_file))

    first = db.reconcile_video_presence(creator["id"], [], confirmation_scans=2)
    assert first == []
    assert db.get_video(video_id)["remote_status"] == "unconfirmed_missing"

    second = db.reconcile_video_presence(creator["id"], [], confirmation_scans=2)
    assert [item["aweme_id"] for item in second] == ["removed-1"]
    video = db.get_video(video_id)
    assert video["remote_status"] == "removed_or_private"
    assert video["status"] == "downloaded"
    assert local_file.exists()


def test_interrupted_download_is_requeued_on_startup(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/test", 60)
    video_id, _ = db.upsert_video(
        creator["id"], {"aweme_id": "resume-1", "description": "", "raw": {}}
    )
    db.update_video(video_id, status="downloading", bytes_downloaded=1024)
    db.update_creator(creator["id"], status="downloading", next_scan_at="2999-01-01T00:00:00+00:00")

    db.initialize()

    video = db.get_video(video_id)
    recovered_creator = db.get_creator(creator["id"])
    assert video["status"] == "pending"
    assert "恢复到下载队列" in video["last_error"]
    assert recovered_creator["status"] == "idle"
    assert recovered_creator["next_scan_at"] < "2999"


def test_legacy_image_note_is_reclassified_and_requeued(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/test", 60)
    video_id, _ = db.upsert_video(
        creator["id"],
        {
            "aweme_id": "note-1",
            "description": "图文",
            "raw": {"images": [{"url_list": ["https://cdn.test/1.webp"]}]},
        },
    )
    legacy = tmp_path / "note.mp4"
    legacy.write_bytes(b"legacy soundtrack")
    db.update_video(video_id, status="downloaded", file_path=str(legacy))

    db.initialize()

    note = db.get_video(video_id)
    assert note["content_type"] == "images"
    assert note["asset_count"] == 1
    assert note["status"] == "pending"
    assert "重新下载原图" in note["last_error"]


def test_legacy_sqlite_schema_is_upgraded_in_place(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE creators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_url TEXT NOT NULL UNIQUE,
            sec_uid TEXT,
            nickname TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            interval_minutes INTEGER NOT NULL DEFAULT 60,
            last_scan_at TEXT,
            next_scan_at TEXT,
            latest_publish_at TEXT,
            total_found INTEGER NOT NULL DEFAULT 0,
            downloaded_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'idle',
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
            aweme_id TEXT NOT NULL,
            description TEXT,
            create_time INTEGER,
            video_url TEXT,
            cover_url TEXT,
            share_url TEXT,
            content_type TEXT NOT NULL DEFAULT 'video',
            asset_count INTEGER NOT NULL DEFAULT 1,
            is_daily INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            file_path TEXT,
            cover_path TEXT,
            file_size INTEGER,
            bytes_downloaded INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER,
            remote_status TEXT NOT NULL DEFAULT 'active',
            missing_count INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT,
            remote_changed_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            raw_json TEXT,
            discovered_at TEXT NOT NULL,
            downloaded_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(creator_id, aweme_id)
        );
        CREATE TABLE event_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            creator_id INTEGER,
            video_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO creators(profile_url, created_at, updated_at)
        VALUES ('https://www.douyin.com/user/legacy', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        INSERT INTO videos(creator_id, aweme_id, discovered_at, updated_at)
        VALUES (1, 'legacy-video', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    db = Database(path)
    db.initialize()

    creator = db.get_creator(1)
    video = db.get_video(1)
    assert creator["download_policy"] == "metadata_only"
    assert creator["per_scan_limit"] == 100
    assert video["created_at"].startswith("2026-01-01")


def test_raw_delete_keeps_foreign_key_cascade(tmp_path: Path) -> None:
    db = Database(tmp_path / "cascade.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/cascade", 60)
    db.upsert_video(creator["id"], {"aweme_id": "cascade-video", "raw": {}})

    db.execute("DELETE FROM creators WHERE id = ?", (creator["id"],))

    assert db.fetch_one("SELECT id FROM videos WHERE aweme_id = ?", ("cascade-video",)) is None


def test_scan_job_lifecycle_and_owner_deduplication(tmp_path: Path) -> None:
    db = Database(tmp_path / "scan-jobs.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/scan-job", 60)

    first, created = db.create_scan_job(
        creator_id=creator["id"],
        job_type="initial",
        item_limit=100,
        max_scrolls=50,
        max_runtime_seconds=300,
    )
    duplicate, duplicate_created = db.create_scan_job(
        creator_id=creator["id"], job_type="incremental"
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate["id"] == first["id"]
    paused = db.request_scan_job_pause(first["id"])
    assert paused["status"] == "paused"
    resumed = db.resume_scan_job(first["id"])
    assert resumed["status"] == "queued"
    running = db.claim_scan_job(first["id"])
    assert running is not None
    assert running["status"] == "running"
    updated = db.update_scan_job(
        first["id"],
        scroll_count=12,
        discovered_count=40,
        written_count=20,
        cursor="cursor-1",
        progress_json={"page": 2},
    )
    assert updated["scroll_count"] == 12
    assert updated["progress_json"] == {"page": 2}
    cancelling = db.request_scan_job_cancel(first["id"])
    assert cancelling["status"] == "cancelling"


def test_running_scan_job_is_recovered_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "scan-recovery.db"
    db = Database(path)
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/recover-scan", 60)
    job, _ = db.create_scan_job(creator_id=creator["id"], job_type="incremental")
    db.claim_scan_job(job["id"])
    db.close()

    restarted = Database(path)
    restarted.initialize()
    recovered = restarted.get_scan_job(job["id"])

    assert recovered["status"] == "queued"
    assert "异常中断" in recovered["failure_reason"]
    restarted.close()


def test_creator_detail_includes_schedule_and_active_scan_progress(tmp_path: Path) -> None:
    db = Database(tmp_path / "creator-detail.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/detail", 60)
    job, _ = db.create_scan_job(creator_id=creator["id"], job_type="incremental")
    db.claim_scan_job(job["id"])
    db.update_scan_job(job["id"], scroll_count=7, discovered_count=42)

    detail = db.get_creator_detail(creator["id"])
    listed = db.list_creators()[0]
    paged = db.list_creators_page()["items"][0]

    assert detail["schedule"]["schedule_type"] == "minutes"
    assert detail["active_scan_job"]["scroll_count"] == 7
    assert detail["active_scan_job"]["discovered_count"] == 42
    assert listed["active_scan_job"]["id"] == job["id"]
    assert paged["active_scan_job"]["id"] == job["id"]


def test_download_jobs_are_deduplicated_claimed_and_controlled(tmp_path: Path) -> None:
    db = Database(tmp_path / "download-jobs.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/download-jobs", 60)
    videos = db.bulk_upsert_videos(
        creator["id"], [{"aweme_id": "1"}, {"aweme_id": "2"}]
    )

    first = db.enqueue_download_jobs(creator["id"], [videos[0][0]], priority=0)
    duplicate = db.enqueue_download_jobs(creator["id"], [videos[0][0]], priority=5)
    second = db.enqueue_download_jobs(creator["id"], [videos[1][0]], priority=10)

    assert first[0]["id"] == duplicate[0]["id"]
    assert duplicate[0]["priority"] == 5
    listed = db.list_download_jobs(creator_id=creator["id"])
    assert listed[0]["aweme_id"] == "2"
    assert listed[0]["creator_nickname"] is None
    claimed = db.claim_download_jobs("worker-1", limit=1)
    assert claimed[0]["id"] == second[0]["id"]
    assert claimed[0]["status"] == "running"
    assert claimed[0]["attempts"] == 1

    pausing = db.request_download_job_pause(claimed[0]["id"])
    assert pausing["status"] == "pausing"
    db.update_download_job(claimed[0]["id"], status="paused", locked_by=None, locked_at=None)
    resumed = db.resume_download_job(claimed[0]["id"])
    assert resumed["status"] == "queued"

    cancelled = db.request_download_job_cancel(first[0]["id"])
    assert cancelled["status"] == "cancelled"
    retried = db.retry_download_job(first[0]["id"], priority=20)
    assert retried["status"] == "queued"
    assert retried["priority"] == 20


def test_download_job_list_page_filters_and_paginates(tmp_path: Path) -> None:
    db = Database(tmp_path / "download-job-page.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/download-job-page", 60)
    other = db.add_creator("https://www.douyin.com/user/download-job-page-other", 60)
    video_ids = [
        video_id
        for video_id, _ in db.bulk_upsert_videos(
            creator["id"], [{"aweme_id": f"page-{index}"} for index in range(35)]
        )
    ]
    other_video_id, _ = db.upsert_video(other["id"], {"aweme_id": "other-page"})
    jobs = db.enqueue_download_jobs(creator["id"], video_ids)
    db.enqueue_download_jobs(other["id"], [other_video_id])
    for job in jobs[:3]:
        db.update_download_job(job["id"], status="failed")

    first = db.list_download_jobs_page(
        creator_id=creator["id"], page=1, page_size=30
    )
    second = db.list_download_jobs_page(
        creator_id=creator["id"], page=2, page_size=30
    )
    failed = db.list_download_jobs_page(
        creator_id=creator["id"], statuses={"failed"}
    )

    assert first["total"] == 35
    assert first["total_pages"] == 2
    assert len(first["items"]) == 30
    assert len(second["items"]) == 5
    assert failed["total"] == 3
    assert all(item["status"] == "failed" for item in failed["items"])
    assert db.list_download_jobs_page(page_size=500)["page_size"] == 100


def test_running_download_job_is_recovered_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "download-recovery.db"
    db = Database(path)
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/recover-download", 60)
    video_id, _ = db.upsert_video(creator["id"], {"aweme_id": "1"})
    job = db.enqueue_download_jobs(creator["id"], [video_id])[0]
    db.claim_download_jobs("worker-before-restart")
    db.close()

    restarted = Database(path)
    restarted.initialize()
    recovered = restarted.get_download_job(job["id"])

    assert recovered["status"] == "queued"
    assert recovered["locked_by"] is None
    assert "异常中断" in recovered["failure_reason"]
    restarted.close()

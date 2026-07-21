import asyncio
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.douyin import ScanResult, VerificationRequired
from app.service import SubscriptionService


class VerificationScanner:
    async def scan_profile(self, _: str, **_kwargs):
        raise VerificationRequired("需要滑块验证")


class SuccessfulScanner:
    async def scan_profile(self, profile_url: str, **kwargs):
        items = [
            {
                "aweme_id": "video-1",
                "description": "测试作品",
                "create_time": 1_700_000_000,
                "raw": {"aweme_id": "video-1"},
            }
        ]
        if kwargs.get("on_batch"):
            await kwargs["on_batch"](items)
        if kwargs.get("on_progress"):
            await kwargs["on_progress"](
                {
                    "scroll_count": 2,
                    "discovered_count": 1,
                    "cursor": "cursor-1",
                    "has_more": False,
                }
            )
        return ScanResult(
            profile_url=profile_url,
            nickname="测试用户",
            sec_uid="sec-test",
            videos=[] if kwargs.get("on_batch") else items,
            complete=True,
            expected_count=1,
            aweme_ids=["video-1"],
            latest_create_time=1_700_000_000,
            scroll_count=2,
            cursor="cursor-1",
            stop_reason="complete",
        )

    async def resolve_video(self, *_args, **_kwargs):
        return None


class ContinuationScanner:
    def __init__(self):
        self.skipped: set[str] = set()

    async def scan_profile(self, profile_url: str, **kwargs):
        self.skipped = set(kwargs.get("skip_aweme_ids") or set())
        item = {"aweme_id": "video-2", "description": "更早作品", "create_time": 100}
        await kwargs["on_batch"]([item])
        return ScanResult(
            profile_url=profile_url,
            nickname="测试用户",
            sec_uid="sec-test",
            videos=[],
            complete=False,
            aweme_ids=["video-2"],
            encountered_aweme_ids=["video-1", "video-2"],
            latest_create_time=100,
            scroll_count=5,
            stop_reason="item_limit",
        )


class NoopDownloader:
    pass


class SuccessfulDownloader:
    async def download(self, _creator, _video, progress=None):
        if progress:
            progress(512, 1024)
            progress(1024, 1024)
        return {"file_path": "saved.mp4", "cover_path": None, "file_size": 1024}


class FailingDownloader:
    async def download(self, _creator, _video, progress=None):
        if progress:
            progress(128, 1024)
        raise RuntimeError("temporary network failure")


class VerificationDownloader:
    async def download(self, _creator, _video, progress=None):
        raise VerificationRequired("下载页需要验证码")


class NoopNotifier:
    async def send(self, *_args, **_kwargs):
        return False


def test_verification_pauses_automatic_scans(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/test", 60)
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, VerificationScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    asyncio.run(service.scan_creator(creator["id"]))

    updated = db.get_creator(creator["id"])
    assert updated["status"] == "needs_verification"
    assert "滑块验证" in updated["last_error"]
    job = db.list_scan_jobs(creator_id=creator["id"])[0]
    assert job["status"] == "paused"
    assert "滑块验证" in job["failure_reason"]
    asyncio.run(service.scan_due_creators())
    assert service.task_running(creator["id"]) is False


def test_successful_scan_persists_job_counts(tmp_path: Path) -> None:
    db = Database(tmp_path / "successful-scan.db")
    db.initialize()
    creator = db.add_creator(
        "https://www.douyin.com/user/success",
        60,
        download_policy="all_history_then_auto_new",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    asyncio.run(service.scan_creator(creator["id"]))

    job = db.list_scan_jobs(creator_id=creator["id"])[0]
    assert job["job_type"] == "initial"
    assert job["status"] == "completed"
    assert job["discovered_count"] == 1
    assert job["written_count"] == 1
    assert job["scroll_count"] == 2
    assert job["cursor"] == "cursor-1"
    assert job["started_at"] is not None
    assert job["ended_at"] is not None
    assert db.list_videos(creator_id=creator["id"])[0]["aweme_id"] == "video-1"
    assert db.list_download_jobs(creator_id=creator["id"])[0]["status"] == "queued"


def test_foreign_resolved_item_is_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, VerificationScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]
    assert service._is_foreign_item(
        {"sec_uid": "other", "raw": {}}, "target"
    ) is True
    assert service._is_foreign_item(
        {"sec_uid": "target", "raw": {}}, "target"
    ) is False


def test_scan_job_pause_cancel_and_resume_service_methods(tmp_path: Path) -> None:
    db = Database(tmp_path / "scan-controls.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/scan-controls", 60)
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]
    job, _ = db.create_scan_job(creator_id=creator["id"], job_type="incremental")

    paused = service.pause_scan_job(job["id"])
    assert paused["status"] == "paused"

    async def resume_and_wait() -> dict:
        resumed = service.resume_scan_job(job["id"])
        task = service._tasks[creator["id"]]
        await task
        return resumed

    resumed = asyncio.run(resume_and_wait())
    assert resumed["started"] is True
    assert db.get_scan_job(job["id"])["status"] == "completed"

    second, _ = db.create_scan_job(creator_id=creator["id"], job_type="incremental")
    cancelled = service.cancel_scan_job(second["id"])
    assert cancelled["status"] == "cancelled"


def test_continue_scan_skips_existing_video_ids(tmp_path: Path) -> None:
    db = Database(tmp_path / "continue-scan.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/continue", 60)
    db.upsert_video(creator["id"], {"aweme_id": "video-1"})
    scanner = ContinuationScanner()
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, scanner, NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]
    job, _ = db.create_scan_job(
        creator_id=creator["id"], job_type="continue", item_limit=1
    )

    asyncio.run(service.scan_creator(creator["id"], job["id"]))

    assert scanner.skipped == {"video-1"}
    assert db.list_video_aweme_ids(creator["id"]) == ["video-1", "video-2"]
    saved_job = db.get_scan_job(job["id"])
    assert saved_job["job_type"] == "continue"
    assert saved_job["written_count"] == 1


def test_background_download_worker_completes_queued_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "download-worker.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/download-worker", 60)
    video_id, _ = db.upsert_video(
        creator["id"],
        {"aweme_id": "video-1", "video_url": "https://example.test/video.mp4"},
    )
    job = db.enqueue_download_jobs(creator["id"], [video_id], max_attempts=1)[0]
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
        download_concurrency=1,
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), SuccessfulDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    async def run() -> None:
        await service.start()
        service.wake_download_workers()
        for _ in range(100):
            if db.get_download_job(job["id"])["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        await service.shutdown()

    asyncio.run(run())

    saved_job = db.get_download_job(job["id"])
    video = db.get_video(video_id)
    assert saved_job["status"] == "completed"
    assert saved_job["bytes_downloaded"] == 1024
    assert video["status"] == "downloaded"
    assert video["file_path"] == "saved.mp4"


def test_download_worker_retries_then_marks_failure(tmp_path: Path) -> None:
    db = Database(tmp_path / "download-retry.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/download-retry", 60)
    video_id, _ = db.upsert_video(
        creator["id"],
        {"aweme_id": "video-1", "video_url": "https://example.test/video.mp4"},
    )
    job = db.enqueue_download_jobs(creator["id"], [video_id], max_attempts=2)[0]
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
        download_concurrency=1,
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), FailingDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    async def run() -> None:
        await service.start()
        service.wake_download_workers()
        for _ in range(200):
            if db.get_download_job(job["id"])["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        await service.shutdown()

    asyncio.run(run())

    saved_job = db.get_download_job(job["id"])
    assert saved_job["status"] == "failed"
    assert saved_job["attempts"] == 2
    assert "temporary network failure" in saved_job["failure_reason"]
    assert db.get_video(video_id)["status"] == "failed"


def test_download_verification_pauses_without_retry(tmp_path: Path) -> None:
    db = Database(tmp_path / "download-verification.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/download-verification", 60)
    video_id, _ = db.upsert_video(
        creator["id"],
        {"aweme_id": "video-1", "video_url": "https://example.test/video.mp4"},
    )
    job = db.enqueue_download_jobs(creator["id"], [video_id], max_attempts=5)[0]
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), VerificationDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    async def run() -> None:
        await service.start()
        service.wake_download_workers()
        for _ in range(100):
            if db.get_download_job(job["id"])["status"] == "paused":
                break
            await asyncio.sleep(0.01)
        await service.shutdown()

    asyncio.run(run())

    saved_job = db.get_download_job(job["id"])
    assert saved_job["status"] == "paused"
    assert saved_job["attempts"] == 1
    assert "验证码" in saved_job["failure_reason"]


def test_download_job_service_controls(tmp_path: Path) -> None:
    db = Database(tmp_path / "download-controls.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/download-controls", 60)
    video_id, _ = db.upsert_video(creator["id"], {"aweme_id": "video-1"})
    job = db.enqueue_download_jobs(creator["id"], [video_id])[0]
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    assert service.pause_download_job(job["id"])["status"] == "paused"
    assert service.resume_download_job(job["id"])["status"] == "queued"
    assert service.cancel_download_job(job["id"])["status"] == "cancelled"
    assert service.retry_download_job(job["id"])["status"] == "queued"


def test_manual_video_downloads_are_grouped_and_prioritized(tmp_path: Path) -> None:
    db = Database(tmp_path / "manual-downloads.db")
    db.initialize()
    first_creator = db.add_creator("https://www.douyin.com/user/manual-1", 60)
    second_creator = db.add_creator("https://www.douyin.com/user/manual-2", 60)
    first_video, _ = db.upsert_video(first_creator["id"], {"aweme_id": "manual-1"})
    second_video, _ = db.upsert_video(second_creator["id"], {"aweme_id": "manual-2"})
    db.update_video(first_video, status="failed", last_error="old failure")
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    jobs = service.queue_manual_video_downloads([first_video, second_video, first_video])

    assert len(jobs) == 2
    assert all(job["priority"] == 100 for job in jobs)
    assert all(job["status"] == "queued" for job in jobs)
    assert db.get_video(first_video)["status"] == "pending"
    assert db.get_video(first_video)["last_error"] is None


def test_bulk_retry_only_queues_failed_videos(tmp_path: Path) -> None:
    db = Database(tmp_path / "bulk-retry.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/bulk-retry", 60)
    failed_video, _ = db.upsert_video(creator["id"], {"aweme_id": "failed"})
    pending_video, _ = db.upsert_video(creator["id"], {"aweme_id": "pending"})
    db.update_video(failed_video, status="failed", last_error="network")
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    result = service.retry_failed_videos([failed_video, pending_video])

    assert result["queued_count"] == 1
    assert result["skipped_count"] == 1
    assert result["download_jobs"][0]["video_id"] == failed_video
    assert db.get_video(failed_video)["status"] == "pending"
    assert db.list_download_jobs(creator_id=creator["id"])[0]["priority"] == 100


def test_pending_confirmations_can_download_or_keep_metadata(tmp_path: Path) -> None:
    db = Database(tmp_path / "pending-confirmations.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/pending-confirmations", 60)
    download_id, _ = db.upsert_video(creator["id"], {"aweme_id": "download"})
    metadata_id, _ = db.upsert_video(creator["id"], {"aweme_id": "metadata"})
    db.bulk_update_videos([
        {"id": download_id, "needs_confirmation": True},
        {"id": metadata_id, "needs_confirmation": True},
    ])
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    downloaded = service.resolve_pending_confirmations([download_id], download=True)
    kept = service.resolve_pending_confirmations([metadata_id], download=False)

    assert downloaded["resolved_count"] == 1
    assert downloaded["queued_count"] == 1
    assert kept["resolved_count"] == 1
    assert kept["queued_count"] == 0
    assert db.get_video(download_id)["needs_confirmation"] is False
    assert db.get_video(metadata_id)["needs_confirmation"] is False
    assert db.list_download_jobs(creator_id=creator["id"])[0]["video_id"] == download_id


def test_delete_video_keeps_files_by_default_and_recounts(tmp_path: Path) -> None:
    db = Database(tmp_path / "delete-video.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/delete-video", 60)
    video_id, _ = db.upsert_video(creator["id"], {"aweme_id": "delete-me"})
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    settings.download_dir.mkdir(parents=True)
    local_file = settings.download_dir / "saved.mp4"
    local_file.write_bytes(b"video")
    db.update_video(video_id, status="downloaded", file_path=str(local_file))
    db.recount_creator(creator["id"])
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    result = asyncio.run(service.delete_video(video_id))

    assert result["local_files_deleted"] is False
    assert local_file.exists()
    assert db.get_creator(creator["id"])["total_found"] == 0
    try:
        db.get_video(video_id)
    except KeyError:
        pass
    else:
        raise AssertionError("video record should be deleted")


def test_delete_video_only_removes_paths_inside_download_root(tmp_path: Path) -> None:
    db = Database(tmp_path / "delete-video-files.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/delete-files", 60)
    video_id, _ = db.upsert_video(creator["id"], {"aweme_id": "inside"})
    outside_id, _ = db.upsert_video(creator["id"], {"aweme_id": "outside"})
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    image_dir = settings.download_dir / "creator" / "images" / "inside"
    image_dir.mkdir(parents=True)
    cover = image_dir / "1.jpg"
    cover.write_bytes(b"image")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    db.update_video(video_id, status="downloaded", file_path=str(image_dir), cover_path=str(cover))
    db.update_video(outside_id, status="downloaded", file_path=str(outside))
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    asyncio.run(service.delete_video(video_id, delete_local_files=True))
    assert not image_dir.exists()

    try:
        asyncio.run(service.delete_video(outside_id, delete_local_files=True))
    except ValueError as exc:
        assert "下载目录之外" in str(exc)
    else:
        raise AssertionError("outside path deletion should be rejected")
    assert outside.exists()
    assert db.get_video(outside_id)["aweme_id"] == "outside"


def test_delete_creator_stops_jobs_and_keeps_files_by_default(tmp_path: Path) -> None:
    db = Database(tmp_path / "delete-creator.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/delete-creator", 60)
    video_id, _ = db.upsert_video(creator["id"], {"aweme_id": "kept"})
    db.create_scan_job(creator_id=creator["id"], job_type="incremental")
    db.enqueue_download_jobs(creator["id"], [video_id])
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    settings.download_dir.mkdir(parents=True)
    local_file = settings.download_dir / "kept.mp4"
    local_file.write_bytes(b"video")
    db.update_video(video_id, status="downloaded", file_path=str(local_file))
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    result = asyncio.run(service.delete_creator(creator["id"]))

    assert result["video_count"] == 1
    assert result["downloaded_count"] == 1
    assert result["local_files_deleted"] is False
    assert local_file.exists()
    try:
        db.get_creator(creator["id"])
    except KeyError:
        pass
    else:
        raise AssertionError("creator record should be deleted")
    logs = db.list_logs(limit=10)
    assert any("本地文件已保留" in item["message"] for item in logs)


def test_delete_creator_validates_all_paths_before_removing_files(tmp_path: Path) -> None:
    db = Database(tmp_path / "delete-creator-files.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/delete-creator-files", 60)
    inside_id, _ = db.upsert_video(creator["id"], {"aweme_id": "inside"})
    outside_id, _ = db.upsert_video(creator["id"], {"aweme_id": "outside"})
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    settings.download_dir.mkdir(parents=True)
    inside = settings.download_dir / "inside.mp4"
    outside = tmp_path / "outside.mp4"
    inside.write_bytes(b"inside")
    outside.write_bytes(b"outside")
    db.update_video(inside_id, status="downloaded", file_path=str(inside))
    db.update_video(outside_id, status="downloaded", file_path=str(outside))
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    try:
        asyncio.run(service.delete_creator(creator["id"], delete_local_files=True))
    except ValueError as exc:
        assert "下载目录之外" in str(exc)
    else:
        raise AssertionError("outside path deletion should be rejected")

    assert inside.exists()
    assert outside.exists()
    assert db.get_creator(creator["id"])["status"] == "deleting"


def test_due_schedule_advances_when_scan_starts(tmp_path: Path) -> None:
    db = Database(tmp_path / "due-schedule.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/due-schedule", 60)
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    async def run() -> None:
        await service.scan_due_creators()
        await asyncio.gather(*service._tasks.values())

    asyncio.run(run())

    schedule = db.get_creator_schedule(creator["id"])
    assert schedule["last_run_at"] is not None
    assert schedule["next_run_at"] > schedule["last_run_at"]


def test_preview_scan_persists_profile_and_videos(tmp_path: Path) -> None:
    db = Database(tmp_path / "preview-service.db")
    db.initialize()
    preview = db.create_preview_session("https://www.douyin.com/user/preview")
    job, _ = db.create_scan_job(
        preview_session_id=preview["id"], job_type="preview", item_limit=100
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    asyncio.run(service.scan_preview_session(preview["id"], job["id"]))

    saved = db.get_preview_session(preview["token"])
    videos = db.list_preview_videos(preview["token"])
    assert saved["status"] == "completed"
    assert saved["normalized_url"] == preview["submitted_url"]
    assert saved["nickname"] == "测试用户"
    assert saved["sec_uid"] == "sec-test"
    assert saved["discovered_count"] == 1
    assert videos["items"][0]["aweme_id"] == "video-1"
    assert db.get_scan_job(job["id"])["status"] == "completed"


def test_preview_scan_detects_existing_creator(tmp_path: Path) -> None:
    db = Database(tmp_path / "preview-duplicate.db")
    db.initialize()
    profile_url = "https://www.douyin.com/user/duplicate"
    db.add_creator(profile_url, 60)
    preview = db.create_preview_session(profile_url)
    job, _ = db.create_scan_job(
        preview_session_id=preview["id"], job_type="preview", item_limit=100
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    asyncio.run(service.scan_preview_session(preview["id"], job["id"]))

    saved = db.get_preview_session(preview["token"])
    assert saved["status"] == "duplicate"
    assert "监控列表" in saved["last_error"]


def test_download_policy_only_affects_new_scan_results(tmp_path: Path) -> None:
    db = Database(tmp_path / "download-policy.db")
    db.initialize()
    creator = db.add_creator(
        "https://www.douyin.com/user/download-policy",
        60,
        download_policy="new_pending_confirmation",
    )
    db.update_creator(creator["id"], total_found=1)
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    service = SubscriptionService(
        db, SuccessfulScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    asyncio.run(service.scan_creator(creator["id"]))

    video = db.list_videos(creator_id=creator["id"])[0]
    assert video["policy_snapshot"] == "new_pending_confirmation"
    assert video["needs_confirmation"] is True
    assert db.list_download_jobs(creator_id=creator["id"]) == []
    assert db.get_creator(creator["id"])["pending_confirmation_count"] == 1

    db.update_creator(
        creator["id"],
        download_policy="all_history_then_auto_new",
        policy_changed_at="2026-07-21T00:00:00+00:00",
    )
    assert db.list_download_jobs(creator_id=creator["id"]) == []

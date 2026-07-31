"""长期无人值守运行的稳定性回归：崩溃自愈、扫描不堆积、历史数据不无限增长。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db import Database
from app.douyin import ScanResult
from app.linux_runtime import LinuxRuntime
from app.service import SubscriptionService


class BlockingScanner:
    """一直卡住的扫描器，用来观察派发行为而不是扫描结果。"""

    def __init__(self) -> None:
        self.started = 0
        self.release = asyncio.Event()

    async def scan_profile(self, profile_url: str, **_kwargs):
        self.started += 1
        await self.release.wait()
        return ScanResult(
            profile_url=profile_url,
            nickname="测试用户",
            sec_uid="sec-test",
            videos=[],
            complete=True,
            aweme_ids=[],
            scroll_count=0,
            stop_reason="complete",
        )

    async def resolve_video(self, *_args, **_kwargs):
        return None


class NoopDownloader:
    pass


class NoopNotifier:
    async def send(self, *_args, **_kwargs):
        return False


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
        **overrides,
    )


def _due_creators(db: Database, count: int) -> list[int]:
    creator_ids: list[int] = []
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    for index in range(count):
        creator = db.add_creator(f"https://www.douyin.com/user/stability-{index}", 60)
        db.update_creator_schedule(
            creator["id"],
            schedule_type="hours",
            interval_value=1,
            daily_time=None,
            timezone_name="Asia/Shanghai",
            jitter_seconds=0,
            after=past - timedelta(hours=1),
        )
        creator_ids.append(int(creator["id"]))
    return creator_ids


# ---------------------------------------------------------------------------
# 扫描派发
# ---------------------------------------------------------------------------


def test_due_scans_are_dispatched_in_batches_instead_of_all_at_once(tmp_path: Path) -> None:
    db = Database(tmp_path / "stampede.db")
    db.initialize()
    creator_ids = _due_creators(db, 5)
    scanner = BlockingScanner()
    service = SubscriptionService(
        db, scanner, NoopDownloader(), _settings(tmp_path, scan_concurrency=1), NoopNotifier()
    )  # type: ignore[arg-type]

    async def run() -> None:
        await service.scan_due_creators()
        await asyncio.sleep(0)
        # 额度是 1，第一轮只应该有一个主播真正开跑。
        assert service.active_scan_count() == 1
        # 再来一轮轮询也不能突破额度。
        await service.scan_due_creators()
        await asyncio.sleep(0)
        assert service.active_scan_count() == 1
        scanner.release.set()
        await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)

    asyncio.run(run())

    scanning = [
        creator_id
        for creator_id in creator_ids
        if db.get_creator(creator_id)["status"] == "scanning"
    ]
    assert scanning == []
    db.close()


def test_dispatch_stops_while_the_browser_runtime_is_down(tmp_path: Path) -> None:
    db = Database(tmp_path / "runtime-down.db")
    db.initialize()
    _due_creators(db, 3)
    scanner = BlockingScanner()
    service = SubscriptionService(
        db, scanner, NoopDownloader(), _settings(tmp_path), NoopNotifier()
    )  # type: ignore[arg-type]
    service.set_runtime_probe(lambda: False)

    asyncio.run(service.scan_due_creators())

    assert service.active_scan_count() == 0
    assert scanner.started == 0
    db.close()


def test_queued_jobs_resume_within_the_concurrency_budget(tmp_path: Path) -> None:
    db = Database(tmp_path / "resume.db")
    db.initialize()
    creator_ids = _due_creators(db, 4)
    for creator_id in creator_ids:
        db.create_scan_job(creator_id=creator_id, job_type="incremental")
    scanner = BlockingScanner()
    service = SubscriptionService(
        db, scanner, NoopDownloader(), _settings(tmp_path, scan_concurrency=2), NoopNotifier()
    )  # type: ignore[arg-type]

    async def run() -> None:
        await service.resume_pending_scan_jobs()
        await asyncio.sleep(0)
        assert service.active_scan_count() == 2
        scanner.release.set()
        await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)

    asyncio.run(run())
    db.close()


# ---------------------------------------------------------------------------
# 历史数据回收
# ---------------------------------------------------------------------------


def test_prune_history_drops_old_logs_and_finished_scan_jobs(tmp_path: Path) -> None:
    db = Database(tmp_path / "retention.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/retention", 60)
    job, _ = db.create_scan_job(creator_id=creator["id"], job_type="incremental")
    db.update_scan_job(int(job["id"]), status="completed")
    active, _ = db.create_scan_job(creator_id=creator["id"], job_type="incremental")
    for index in range(5):
        db.add_log("info", f"历史日志 {index}")

    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)
    with db.session() as session:
        session.execute(text("UPDATE event_logs SET created_at = :stamp"), {"stamp": stale})
        session.execute(
            text("UPDATE scan_jobs SET updated_at = :stamp WHERE status = 'completed'"),
            {"stamp": stale},
        )

    removed = db.prune_history(log_retention_days=30, log_max_rows=0, job_retention_days=30)

    assert removed["event_logs"] == 5
    assert removed["scan_jobs"] == 1
    assert db.list_logs(limit=100) == []
    remaining = {int(item["id"]) for item in db.list_scan_jobs(creator_id=creator["id"])}
    assert remaining == {int(active["id"])}
    db.close()


def test_prune_history_caps_logs_by_row_count(tmp_path: Path) -> None:
    db = Database(tmp_path / "retention-rows.db")
    db.initialize()
    for index in range(10):
        db.add_log("info", f"日志 {index}")

    removed = db.prune_history(log_retention_days=0, log_max_rows=4, job_retention_days=0)

    assert removed["event_logs"] == 6
    assert len(db.list_logs(limit=100)) == 4
    db.close()


# ---------------------------------------------------------------------------
# 浏览器运行时
# ---------------------------------------------------------------------------


def test_stale_x_lock_is_removed_when_no_server_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = LinuxRuntime(_settings(tmp_path))
    lock = tmp_path / ".X99-lock"
    sock = tmp_path / "X99"
    lock.write_text("     12345\n", encoding="utf-8")
    sock.write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime, "_x_lock_path", lambda: lock)
    monkeypatch.setattr(runtime, "_x_socket_path", lambda: sock)
    monkeypatch.setattr(runtime, "_x_server_ready", lambda: False)

    assert runtime._clear_stale_x_locks() is True
    assert not lock.exists()
    assert not sock.exists()


def test_live_x_server_keeps_its_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = LinuxRuntime(_settings(tmp_path))
    lock = tmp_path / ".X99-lock"
    lock.write_text("     12345\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_x_lock_path", lambda: lock)
    monkeypatch.setattr(runtime, "_x_socket_path", lambda: tmp_path / "X99")
    monkeypatch.setattr(runtime, "_x_server_ready", lambda: True)

    assert runtime._clear_stale_x_locks() is False
    assert lock.exists()


def test_display_number_is_parsed_from_the_display_name(tmp_path: Path) -> None:
    runtime = LinuxRuntime(_settings(tmp_path, linux_display=":7"))
    assert runtime._display_number() == 7
    assert runtime._x_lock_path() == Path("/tmp/.X7-lock")


def test_chrome_command_bounds_cache_and_disables_throttling(tmp_path: Path) -> None:
    settings = _settings(tmp_path, linux_chromium_disk_cache_mb=64)
    command = LinuxRuntime(settings)._build_chrome_command("chromium")

    assert "--disable-background-timer-throttling" in command
    assert "--disable-renderer-backgrounding" in command
    assert f"--disk-cache-size={64 * 1024 * 1024}" in command
    assert command[-1] == "about:blank"


def test_runtime_reports_not_ready_before_start(tmp_path: Path) -> None:
    runtime = LinuxRuntime(_settings(tmp_path))
    assert runtime.ready is False
    assert runtime.status()["ready"] is False


# ---------------------------------------------------------------------------
# 关闭路径
# ---------------------------------------------------------------------------


def test_shutdown_cancels_downloads_that_outlast_the_grace_period(tmp_path: Path) -> None:
    """下载 worker 只 gather 不 cancel 时，一个大文件能把关闭拖满 stop_grace_period。"""
    db = Database(tmp_path / "shutdown.db")
    db.initialize()
    settings = _settings(tmp_path, shutdown_grace_seconds=1)
    service = SubscriptionService(
        db, BlockingScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]

    async def run() -> None:
        never_finishes = asyncio.Event()
        task = asyncio.create_task(never_finishes.wait())
        service._download_tasks = [task]  # type: ignore[list-item]

        await asyncio.wait_for(service.shutdown(), timeout=10)

        assert task.cancelled() or task.done()
        assert service._download_tasks == []

    asyncio.run(run())
    db.close()


def test_shutdown_waits_for_downloads_that_finish_inside_the_grace_period(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "shutdown-graceful.db")
    db.initialize()
    settings = _settings(tmp_path, shutdown_grace_seconds=10)
    service = SubscriptionService(
        db, BlockingScanner(), NoopDownloader(), settings, NoopNotifier()
    )  # type: ignore[arg-type]
    finished = False

    async def run() -> None:
        nonlocal finished

        async def quick_job() -> None:
            nonlocal finished
            await asyncio.sleep(0.05)
            finished = True

        task = asyncio.create_task(quick_job())
        service._download_tasks = [task]  # type: ignore[list-item]

        await asyncio.wait_for(service.shutdown(), timeout=10)

        assert finished is True
        assert not task.cancelled()

    asyncio.run(run())
    db.close()

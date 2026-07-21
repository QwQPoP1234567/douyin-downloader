from datetime import datetime, timezone
from pathlib import Path

from app.db import Database
from app.scheduling import calculate_next_run


def test_daily_schedule_uses_asia_shanghai_and_jitter() -> None:
    after = datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc)

    next_run = calculate_next_run(
        schedule_type="daily",
        interval_value=1,
        daily_time="14:00",
        timezone_name="Asia/Shanghai",
        after=after,
        jitter_seconds=30,
    )

    assert next_run == datetime(2026, 7, 21, 6, 0, 30)


def test_interval_schedule_supports_hours_and_days() -> None:
    after = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    assert calculate_next_run(
        schedule_type="hours",
        interval_value=3,
        daily_time=None,
        after=after,
    ) == datetime(2026, 7, 20, 3, 0)
    assert calculate_next_run(
        schedule_type="days",
        interval_value=2,
        daily_time=None,
        after=after,
    ) == datetime(2026, 7, 22, 0, 0)


def test_creator_schedule_is_persisted_recalculated_and_disabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "schedules.db")
    db.initialize()
    creator = db.add_creator("https://www.douyin.com/user/schedule", 60)
    initial = db.get_creator_schedule(creator["id"])
    assert initial["schedule_type"] == "minutes"
    assert initial["next_run_at"] is not None

    after = datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc)
    daily = db.update_creator_schedule(
        creator["id"],
        schedule_type="daily",
        interval_value=1,
        daily_time="14:00",
        timezone_name="Asia/Shanghai",
        jitter_seconds=15,
        after=after,
    )
    assert daily["daily_time"] == "14:00:00"
    assert daily["next_run_at"].startswith("2026-07-21T06:00:15")

    due = db.list_due_creator_schedules(
        now=datetime(2026, 7, 21, 6, 0, 15, tzinfo=timezone.utc)
    )
    assert [item["creator_id"] for item in due] == [creator["id"]]

    recorded = db.record_creator_schedule_run(
        creator["id"], run_at=datetime(2026, 7, 21, 6, 0, 15, tzinfo=timezone.utc)
    )
    assert recorded["last_run_at"].startswith("2026-07-21T06:00:15")
    assert recorded["next_run_at"].startswith("2026-07-22T06:00:15")

    disabled = db.set_creator_schedule_enabled(creator["id"], False, after=after)
    assert disabled["enabled"] is False
    assert disabled["next_run_at"] is None
    db.close()

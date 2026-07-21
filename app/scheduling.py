from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEDULE_TYPES = {"minutes", "hours", "daily", "days"}


def validate_schedule(
    schedule_type: str,
    interval_value: int,
    daily_time: str | None,
    timezone_name: str,
) -> tuple[str, int, str | None, str]:
    normalized_type = schedule_type.strip().lower()
    if normalized_type not in SCHEDULE_TYPES:
        raise ValueError("schedule_type must be minutes, hours, daily, or days")
    interval = int(interval_value)
    if interval < 1:
        raise ValueError("interval_value must be at least 1")
    if normalized_type == "daily":
        if not daily_time:
            raise ValueError("daily_time is required for a daily schedule")
        try:
            parsed_time = time.fromisoformat(daily_time)
        except ValueError as exc:
            raise ValueError("daily_time must use HH:MM or HH:MM:SS") from exc
        normalized_daily_time = parsed_time.replace(microsecond=0).isoformat()
    else:
        normalized_daily_time = None
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    return normalized_type, interval, normalized_daily_time, timezone_name


def calculate_next_run(
    *,
    schedule_type: str,
    interval_value: int,
    daily_time: str | None,
    timezone_name: str = "Asia/Shanghai",
    after: datetime | None = None,
    jitter_seconds: int = 0,
) -> datetime:
    schedule_type, interval, daily_time, timezone_name = validate_schedule(
        schedule_type, interval_value, daily_time, timezone_name
    )
    current = after or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    if schedule_type == "minutes":
        candidate = current + timedelta(minutes=interval)
    elif schedule_type == "hours":
        candidate = current + timedelta(hours=interval)
    elif schedule_type == "days":
        candidate = current + timedelta(days=interval)
    else:
        zone = ZoneInfo(timezone_name)
        local_now = current.astimezone(zone)
        clock = time.fromisoformat(daily_time or "00:00:00")
        candidate_local = datetime.combine(local_now.date(), clock, tzinfo=zone)
        if candidate_local <= local_now:
            candidate_local += timedelta(days=1)
        candidate = candidate_local.astimezone(timezone.utc)

    candidate += timedelta(seconds=max(0, int(jitter_seconds)))
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)

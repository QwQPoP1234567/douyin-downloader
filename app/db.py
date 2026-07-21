from __future__ import annotations

import base64
import binascii
import json
import math
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from sqlalchemy import Engine, and_, case, create_engine, event, func, inspect, or_, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AppSetting,
    Base,
    Creator,
    CreatorSchedule,
    DownloadJob,
    EventLog,
    PreviewSession,
    PreviewVideo,
    ScanJob,
    Video,
    VideoAsset,
    utc_datetime,
)
from app.scheduling import calculate_next_run, validate_schedule
from app.policies import should_download_selected_on_confirm, validate_download_policy


T = TypeVar("T")

DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "last_scan_at",
    "next_scan_at",
    "latest_publish_at",
    "policy_changed_at",
    "discovered_at",
    "downloaded_at",
    "last_seen_at",
    "remote_changed_at",
    "started_at",
    "ended_at",
    "heartbeat_at",
    "next_attempt_at",
    "locked_at",
    "finished_at",
    "last_run_at",
    "next_run_at",
    "expires_at",
    "confirmed_at",
}

ACTIVE_SCAN_JOB_STATUSES = {"queued", "running", "pausing", "paused", "cancelling"}
TERMINAL_SCAN_JOB_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_DOWNLOAD_JOB_STATUSES = {"queued", "running", "pausing", "paused", "cancelling"}
TERMINAL_DOWNLOAD_JOB_STATUSES = {"completed", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_utc_datetime(value: Any) -> Any:
    if not isinstance(value, str) or len(value) < 10 or value[4:5] != "-":
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return value


def _mapping_to_dict(row: RowMapping) -> dict[str, Any]:
    return {key: _serialize(value) for key, value in row.items()}


class Database:
    """Short-lived SQLAlchemy sessions with a compatibility API for the service layer."""

    def __init__(
        self,
        target: Path | str,
        *,
        pool_size: int = 3,
        max_overflow: int = 1,
        pool_recycle_seconds: int = 1800,
        connect_retries: int = 3,
    ):
        if isinstance(target, Path):
            target.parent.mkdir(parents=True, exist_ok=True)
            self.url = f"sqlite:///{target.as_posix()}"
            self.path: Path | None = target
        else:
            self.url = target
            self.path = None
        self.connect_retries = max(1, connect_retries)
        self._lock = threading.RLock()

        engine_options: dict[str, Any] = {
            "pool_pre_ping": True,
            "pool_recycle": pool_recycle_seconds,
        }
        if self.url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            engine_options.update(pool_size=pool_size, max_overflow=max_overflow)
        self.engine: Engine = create_engine(self.url, **engine_options)
        if self.url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite_connection)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()

    @property
    def dialect_name(self) -> str:
        return self.engine.dialect.name

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _is_transient(exc: DBAPIError) -> bool:
        if exc.connection_invalidated or isinstance(exc, OperationalError):
            return True
        original = getattr(exc, "orig", None)
        args = getattr(original, "args", ())
        return bool(args and args[0] in {1040, 1042, 1047, 1205, 1213, 2002, 2003, 2006, 2013})

    def _run(self, operation: Callable[[], T]) -> T:
        last_error: DBAPIError | None = None
        for attempt in range(self.connect_retries):
            try:
                return operation()
            except DBAPIError as exc:
                if not self._is_transient(exc) or attempt + 1 >= self.connect_retries:
                    raise
                last_error = exc
                self.engine.dispose()
        assert last_error is not None
        raise last_error

    @staticmethod
    def _prepare_sql(sql: str, params: tuple[Any, ...]) -> tuple[Any, dict[str, Any]]:
        parts = sql.split("?")
        if len(parts) - 1 != len(params):
            if params:
                raise ValueError("SQL placeholder count does not match parameters")
            return text(sql), {}
        if not params:
            return text(sql), {}
        statement = parts[0]
        bound: dict[str, Any] = {}
        for index, value in enumerate(params):
            name = f"p{index}"
            statement += f":{name}{parts[index + 1]}"
            bound[name] = _as_utc_datetime(value)
        return text(statement), bound

    def initialize(self) -> None:
        def operation() -> None:
            with self._lock:
                Base.metadata.create_all(self.engine)
                self._upgrade_legacy_schema()
                self._recover_interrupted_work()

        self._run(operation)

    def _upgrade_legacy_schema(self) -> None:
        definitions: dict[str, dict[str, str]] = {
            "creators": {
                "avatar_url": "TEXT NULL",
                "download_policy": "VARCHAR(64) NOT NULL DEFAULT 'metadata_only'",
                "policy_changed_at": "DATETIME NULL",
                "per_scan_limit": "INTEGER NOT NULL DEFAULT 100",
                "failed_count": "INTEGER NOT NULL DEFAULT 0",
                "pending_confirmation_count": "INTEGER NOT NULL DEFAULT 0",
            },
            "videos": {
                "created_at": "DATETIME NULL",
                "policy_snapshot": "VARCHAR(64) NULL",
                "needs_confirmation": "BOOLEAN NOT NULL DEFAULT 0",
            },
            "event_logs": {
                "job_type": "VARCHAR(32) NULL",
                "job_id": "INTEGER NULL",
                "details_json": "JSON NULL" if self.dialect_name == "mysql" else "TEXT NULL",
            },
        }
        inspector = inspect(self.engine)
        table_names = set(inspector.get_table_names())
        with self.engine.begin() as connection:
            for table_name, columns in definitions.items():
                if table_name not in table_names:
                    continue
                existing = {column["name"] for column in inspector.get_columns(table_name)}
                for column_name, ddl in columns.items():
                    if column_name not in existing:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))
            if "videos" in table_names:
                connection.execute(
                    text("UPDATE videos SET created_at = discovered_at WHERE created_at IS NULL")
                )

    def _recover_interrupted_work(self) -> None:
        now = utc_datetime()
        with self.session() as session:
            creators_without_schedule = session.scalars(
                select(Creator)
                .outerjoin(CreatorSchedule, CreatorSchedule.creator_id == Creator.id)
                .where(CreatorSchedule.id.is_(None))
            ).all()
            for creator in creators_without_schedule:
                session.add(
                    CreatorSchedule(
                        creator_id=creator.id,
                        schedule_type="minutes",
                        interval_value=max(1, int(creator.interval_minutes or 60)),
                        timezone="Asia/Shanghai",
                        enabled=bool(creator.enabled),
                        next_run_at=creator.next_scan_at or now,
                    )
                )
            session.query(ScanJob).filter(
                ScanJob.status.in_(["running", "pausing", "cancelling"])
            ).update(
                {
                    ScanJob.status: "queued",
                    ScanJob.pause_requested: False,
                    ScanJob.cancel_requested: False,
                    ScanJob.failure_reason: "服务异常中断，任务已恢复到待执行队列",
                    ScanJob.heartbeat_at: now,
                    ScanJob.ended_at: None,
                    ScanJob.updated_at: now,
                },
                synchronize_session=False,
            )
            session.query(DownloadJob).filter(
                DownloadJob.status.in_(["running", "pausing", "cancelling"])
            ).update(
                {
                    DownloadJob.status: "queued",
                    DownloadJob.locked_by: None,
                    DownloadJob.locked_at: None,
                    DownloadJob.heartbeat_at: now,
                    DownloadJob.pause_requested: False,
                    DownloadJob.cancel_requested: False,
                    DownloadJob.failure_reason: "服务异常中断，下载任务已恢复到队列",
                    DownloadJob.updated_at: now,
                },
                synchronize_session=False,
            )
            interrupted = session.scalars(
                select(Video.creator_id).where(Video.status == "downloading").distinct()
            ).all()
            session.query(Video).filter(Video.status == "downloading").update(
                {
                    Video.status: "pending",
                    Video.last_error: "上次运行中断，已恢复到下载队列",
                    Video.updated_at: now,
                },
                synchronize_session=False,
            )
            session.query(Creator).filter(Creator.status.in_(["scanning", "downloading"])).update(
                {Creator.status: "idle", Creator.updated_at: now}, synchronize_session=False
            )
            if interrupted:
                session.query(Creator).filter(Creator.id.in_(interrupted)).update(
                    {Creator.status: "idle", Creator.next_scan_at: now, Creator.updated_at: now},
                    synchronize_session=False,
                )
            self._reclassify_legacy_image_posts(session)

    @staticmethod
    def _reclassify_legacy_image_posts(session: Session) -> None:
        for video in session.scalars(select(Video)).yield_per(200):
            try:
                payload = json.loads(video.raw_json or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            images = (
                payload.get("images") or payload.get("image_list") or payload.get("original_images")
            ) if isinstance(payload, dict) else None
            image_post = payload.get("image_post_info") if isinstance(payload, dict) else None
            if not images and isinstance(image_post, dict):
                images = image_post.get("images") or image_post.get("image_list")
            is_daily = bool(
                isinstance(payload, dict)
                and (
                    payload.get("is_story")
                    or payload.get("is_moment_story")
                    or payload.get("is_24_story")
                    or payload.get("is_25_story")
                )
            )
            if isinstance(images, list) and images:
                needs_redownload = bool(
                    video.status == "downloaded"
                    and isinstance(video.file_path, str)
                    and video.file_path.lower().endswith(".mp4")
                )
                video.content_type = "images"
                video.asset_count = len(images)
                video.is_daily = is_daily
                if needs_redownload:
                    video.status = "pending"
                    video.last_error = "已识别为图文/日常，等待重新下载原图"
            elif is_daily:
                video.is_daily = True

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        statement, bound = self._prepare_sql(sql, params)

        def operation() -> int:
            with self.engine.begin() as connection:
                result = connection.execute(statement, bound)
                inserted = getattr(result, "lastrowid", None)
                return int(inserted or result.rowcount or 0)

        return self._run(operation)

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        statement, bound = self._prepare_sql(sql, params)

        def operation() -> dict[str, Any] | None:
            with self.engine.connect() as connection:
                row = connection.execute(statement, bound).mappings().first()
                return _mapping_to_dict(row) if row else None

        return self._run(operation)

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        statement, bound = self._prepare_sql(sql, params)

        def operation() -> list[dict[str, Any]]:
            with self.engine.connect() as connection:
                return [_mapping_to_dict(row) for row in connection.execute(statement, bound).mappings()]

        return self._run(operation)

    @staticmethod
    def _model_dict(model: Any) -> dict[str, Any]:
        return {
            column.name: _serialize(getattr(model, column.name))
            for column in model.__table__.columns
        }

    def add_creator(
        self,
        profile_url: str,
        interval_minutes: int,
        *,
        jitter_seconds: int = 0,
        download_policy: str = "metadata_only",
    ) -> dict[str, Any]:
        download_policy = validate_download_policy(download_policy)

        def operation() -> dict[str, Any]:
            with self.session() as session:
                creator = Creator(
                    profile_url=profile_url,
                    interval_minutes=interval_minutes,
                    next_scan_at=utc_datetime(),
                    download_policy=download_policy,
                    policy_changed_at=utc_datetime(),
                )
                session.add(creator)
                session.flush()
                session.add(
                    CreatorSchedule(
                        creator_id=creator.id,
                        schedule_type="minutes",
                        interval_value=max(1, interval_minutes),
                        timezone="Asia/Shanghai",
                        enabled=True,
                        next_run_at=creator.next_scan_at,
                        jitter_seconds=max(0, int(jitter_seconds)),
                    )
                )
                return self._model_dict(creator)

        return self._run(operation)

    def get_creator_schedule(self, creator_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                schedule = session.scalar(
                    select(CreatorSchedule).where(CreatorSchedule.creator_id == creator_id)
                )
                if schedule is None:
                    raise KeyError(f"Schedule for creator {creator_id} not found")
                return self._model_dict(schedule)

        return self._run(operation)

    def update_creator_schedule(
        self,
        creator_id: int,
        *,
        schedule_type: str,
        interval_value: int,
        daily_time: str | None = None,
        timezone_name: str = "Asia/Shanghai",
        enabled: bool = True,
        jitter_seconds: int | None = None,
        after: datetime | None = None,
    ) -> dict[str, Any]:
        schedule_type, interval_value, daily_time, timezone_name = validate_schedule(
            schedule_type, interval_value, daily_time, timezone_name
        )

        def operation() -> dict[str, Any]:
            with self.session() as session:
                creator = session.scalar(
                    select(Creator).where(Creator.id == creator_id).with_for_update()
                )
                if creator is None:
                    raise KeyError(f"Creator {creator_id} not found")
                schedule = session.scalar(
                    select(CreatorSchedule)
                    .where(CreatorSchedule.creator_id == creator_id)
                    .with_for_update()
                )
                if schedule is None:
                    schedule = CreatorSchedule(creator_id=creator_id)
                    session.add(schedule)
                effective_jitter = max(
                    0,
                    int(
                        jitter_seconds
                        if jitter_seconds is not None
                        else schedule.jitter_seconds or 0
                    ),
                )
                schedule.jitter_seconds = effective_jitter
                schedule.schedule_type = schedule_type
                schedule.interval_value = interval_value
                schedule.daily_time = daily_time
                schedule.timezone = timezone_name
                schedule.enabled = bool(enabled)
                base = after or datetime.now(timezone.utc)
                schedule.next_run_at = (
                    calculate_next_run(
                        schedule_type=schedule_type,
                        interval_value=interval_value,
                        daily_time=daily_time,
                        timezone_name=timezone_name,
                        after=base,
                        jitter_seconds=effective_jitter,
                    )
                    if schedule.enabled and creator.enabled
                    else None
                )
                schedule.updated_at = utc_datetime()
                creator.next_scan_at = schedule.next_run_at
                if schedule_type == "minutes":
                    creator.interval_minutes = interval_value
                elif schedule_type == "hours":
                    creator.interval_minutes = interval_value * 60
                elif schedule_type == "days":
                    creator.interval_minutes = interval_value * 1440
                else:
                    creator.interval_minutes = 1440
                creator.updated_at = utc_datetime()
                session.flush()
                return self._model_dict(schedule)

        return self._run(operation)

    def set_creator_schedule_enabled(
        self, creator_id: int, enabled: bool, *, after: datetime | None = None
    ) -> dict[str, Any]:
        current = self.get_creator_schedule(creator_id)
        return self.update_creator_schedule(
            creator_id,
            schedule_type=str(current["schedule_type"]),
            interval_value=int(current["interval_value"]),
            daily_time=current.get("daily_time"),
            timezone_name=str(current["timezone"]),
            enabled=enabled,
            jitter_seconds=int(current.get("jitter_seconds") or 0),
            after=after,
        )

    def list_due_creator_schedules(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        due_at = now or utc_datetime()
        if due_at.tzinfo is not None:
            due_at = due_at.astimezone(timezone.utc).replace(tzinfo=None)

        def operation() -> list[dict[str, Any]]:
            with self.session() as session:
                rows = session.execute(
                    select(CreatorSchedule, Creator)
                    .join(Creator, Creator.id == CreatorSchedule.creator_id)
                    .where(
                        Creator.enabled.is_(True),
                        CreatorSchedule.enabled.is_(True),
                        CreatorSchedule.next_run_at.is_not(None),
                        CreatorSchedule.next_run_at <= due_at,
                    )
                    .order_by(CreatorSchedule.next_run_at.asc(), CreatorSchedule.id.asc())
                    .limit(max(1, min(limit, 1000)))
                ).all()
                result: list[dict[str, Any]] = []
                for schedule, creator in rows:
                    item = self._model_dict(schedule)
                    item["creator_status"] = creator.status
                    item["creator_enabled"] = creator.enabled
                    result.append(item)
                return result

        return self._run(operation)

    def record_creator_schedule_run(
        self, creator_id: int, *, run_at: datetime | None = None
    ) -> dict[str, Any]:
        run_time = run_at or datetime.now(timezone.utc)
        if run_time.tzinfo is None:
            run_time = run_time.replace(tzinfo=timezone.utc)

        def operation() -> dict[str, Any]:
            with self.session() as session:
                schedule = session.scalar(
                    select(CreatorSchedule)
                    .where(CreatorSchedule.creator_id == creator_id)
                    .with_for_update()
                )
                creator = session.get(Creator, creator_id)
                if schedule is None or creator is None:
                    raise KeyError(f"Schedule for creator {creator_id} not found")
                schedule.last_run_at = run_time.astimezone(timezone.utc).replace(tzinfo=None)
                schedule.next_run_at = (
                    calculate_next_run(
                        schedule_type=schedule.schedule_type,
                        interval_value=schedule.interval_value,
                        daily_time=schedule.daily_time,
                        timezone_name=schedule.timezone,
                        after=run_time,
                        jitter_seconds=schedule.jitter_seconds,
                    )
                    if schedule.enabled and creator.enabled
                    else None
                )
                schedule.updated_at = utc_datetime()
                creator.next_scan_at = schedule.next_run_at
                creator.updated_at = utc_datetime()
                session.flush()
                return self._model_dict(schedule)

        return self._run(operation)

    def create_scan_job(
        self,
        *,
        creator_id: int | None = None,
        preview_session_id: int | None = None,
        job_type: str,
        item_limit: int = 100,
        max_scrolls: int = 300,
        max_runtime_seconds: int = 900,
        cursor: str | None = None,
        progress: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if (creator_id is None) == (preview_session_id is None):
            raise ValueError("A scan job must target exactly one creator or preview session")

        def operation() -> tuple[dict[str, Any], bool]:
            with self._lock, self.session() as session:
                if creator_id is not None:
                    owner = session.scalar(
                        select(Creator).where(Creator.id == creator_id).with_for_update()
                    )
                    if owner is None:
                        raise KeyError(f"Creator {creator_id} not found")
                    active_statement = select(ScanJob).where(
                        ScanJob.creator_id == creator_id,
                        ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES),
                    )
                else:
                    owner = session.scalar(
                        select(PreviewSession)
                        .where(PreviewSession.id == preview_session_id)
                        .with_for_update()
                    )
                    if owner is None:
                        raise KeyError(f"Preview session {preview_session_id} not found")
                    active_statement = select(ScanJob).where(
                        ScanJob.preview_session_id == preview_session_id,
                        ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES),
                    )
                active = session.scalar(active_statement.order_by(ScanJob.id.desc()).limit(1))
                if active is not None:
                    return self._model_dict(active), False
                job = ScanJob(
                    creator_id=creator_id,
                    preview_session_id=preview_session_id,
                    job_type=job_type,
                    status="queued",
                    item_limit=max(1, item_limit),
                    max_scrolls=max(1, max_scrolls),
                    max_runtime_seconds=max(1, max_runtime_seconds),
                    cursor=cursor,
                    progress_json=progress,
                )
                session.add(job)
                session.flush()
                return self._model_dict(job), True

        return self._run(operation)

    def get_scan_job(self, job_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.get(ScanJob, job_id)
                if job is None:
                    raise KeyError(f"Scan job {job_id} not found")
                return self._model_dict(job)

        return self._run(operation)

    def get_active_scan_job(
        self, *, creator_id: int | None = None, preview_session_id: int | None = None
    ) -> dict[str, Any] | None:
        if (creator_id is None) == (preview_session_id is None):
            raise ValueError("Specify exactly one scan owner")

        def operation() -> dict[str, Any] | None:
            with self.session() as session:
                statement = select(ScanJob).where(
                    ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES)
                )
                if creator_id is not None:
                    statement = statement.where(ScanJob.creator_id == creator_id)
                else:
                    statement = statement.where(
                        ScanJob.preview_session_id == preview_session_id
                    )
                job = session.scalar(statement.order_by(ScanJob.id.desc()).limit(1))
                return self._model_dict(job) if job is not None else None

        return self._run(operation)

    def list_scan_jobs(
        self,
        *,
        creator_id: int | None = None,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self.session() as session:
                statement = select(ScanJob)
                if creator_id is not None:
                    statement = statement.where(ScanJob.creator_id == creator_id)
                if statuses:
                    statement = statement.where(ScanJob.status.in_(statuses))
                jobs = session.scalars(
                    statement.order_by(ScanJob.created_at.asc(), ScanJob.id.asc()).limit(
                        max(1, min(limit, 1000))
                    )
                ).all()
                return [self._model_dict(job) for job in jobs]

        return self._run(operation)

    def update_scan_job(self, job_id: int, **values: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "scroll_count",
            "discovered_count",
            "written_count",
            "item_limit",
            "max_scrolls",
            "max_runtime_seconds",
            "cursor",
            "progress_json",
            "started_at",
            "ended_at",
            "heartbeat_at",
            "pause_requested",
            "cancel_requested",
            "failure_reason",
        }
        fields = {key: value for key, value in values.items() if key in allowed}

        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.get(ScanJob, job_id)
                if job is None:
                    raise KeyError(f"Scan job {job_id} not found")
                for key, value in fields.items():
                    setattr(job, key, _as_utc_datetime(value) if key in DATETIME_FIELDS else value)
                job.updated_at = utc_datetime()
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def claim_scan_job(self, job_id: int) -> dict[str, Any] | None:
        def operation() -> dict[str, Any] | None:
            with self.session() as session:
                job = session.scalar(
                    select(ScanJob).where(ScanJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise KeyError(f"Scan job {job_id} not found")
                if job.status != "queued" or job.cancel_requested or job.pause_requested:
                    return None
                now = utc_datetime()
                job.status = "running"
                job.started_at = job.started_at or now
                job.heartbeat_at = now
                job.ended_at = None
                job.updated_at = now
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def request_scan_job_pause(self, job_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.scalar(
                    select(ScanJob).where(ScanJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise KeyError(f"Scan job {job_id} not found")
                if job.status not in TERMINAL_SCAN_JOB_STATUSES:
                    job.pause_requested = True
                    job.status = "paused" if job.status == "queued" else "pausing"
                    job.heartbeat_at = utc_datetime()
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def request_scan_job_cancel(self, job_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.scalar(
                    select(ScanJob).where(ScanJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise KeyError(f"Scan job {job_id} not found")
                if job.status not in TERMINAL_SCAN_JOB_STATUSES:
                    now = utc_datetime()
                    job.cancel_requested = True
                    if job.status in {"queued", "paused"}:
                        job.status = "cancelled"
                        job.ended_at = now
                    else:
                        job.status = "cancelling"
                    job.heartbeat_at = now
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def resume_scan_job(self, job_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self._lock, self.session() as session:
                job = session.scalar(
                    select(ScanJob).where(ScanJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise KeyError(f"Scan job {job_id} not found")
                if job.status not in {"paused", "failed"}:
                    raise ValueError(f"Scan job {job_id} cannot resume from {job.status}")
                if job.creator_id is not None:
                    session.scalar(
                        select(Creator)
                        .where(Creator.id == job.creator_id)
                        .with_for_update()
                    )
                    owner_filter = ScanJob.creator_id == job.creator_id
                else:
                    session.scalar(
                        select(PreviewSession)
                        .where(PreviewSession.id == job.preview_session_id)
                        .with_for_update()
                    )
                    owner_filter = ScanJob.preview_session_id == job.preview_session_id
                other = session.scalar(
                    select(ScanJob).where(
                        owner_filter,
                        ScanJob.id != job.id,
                        ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES),
                    )
                )
                if other is not None:
                    raise RuntimeError("Another scan job is already active for this owner")
                job.status = "queued"
                job.pause_requested = False
                job.cancel_requested = False
                job.failure_reason = None
                job.ended_at = None
                job.updated_at = utc_datetime()
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def create_preview_session(
        self, submitted_url: str, *, expires_in_minutes: int = 120
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                now = utc_datetime()
                preview = PreviewSession(
                    token=secrets.token_urlsafe(32),
                    submitted_url=submitted_url,
                    status="queued",
                    expires_at=now + timedelta(minutes=max(5, expires_in_minutes)),
                )
                session.add(preview)
                session.flush()
                return self._model_dict(preview)

        return self._run(operation)

    def get_preview_session(self, token: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                preview = session.scalar(
                    select(PreviewSession).where(PreviewSession.token == token)
                )
                if preview is None:
                    raise KeyError(f"Preview session {token} not found")
                result = self._model_dict(preview)
                active_job = session.scalar(
                    select(ScanJob)
                    .where(
                        ScanJob.preview_session_id == preview.id,
                        ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES),
                    )
                    .order_by(ScanJob.id.desc())
                    .limit(1)
                )
                result["active_scan_job"] = (
                    self._model_dict(active_job) if active_job is not None else None
                )
                return result

        return self._run(operation)

    def get_preview_session_by_id(self, preview_session_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                preview = session.get(PreviewSession, preview_session_id)
                if preview is None:
                    raise KeyError(f"Preview session {preview_session_id} not found")
                return self._model_dict(preview)

        return self._run(operation)

    def update_preview_session(self, preview_session_id: int, **values: Any) -> dict[str, Any]:
        allowed = {
            "normalized_url",
            "sec_uid",
            "nickname",
            "avatar_url",
            "status",
            "discovered_count",
            "selection_mode",
            "selection_filter",
            "excluded_aweme_ids",
            "selected_aweme_ids",
            "auto_select_new",
            "expires_at",
            "confirmed_at",
            "idempotency_key",
            "last_error",
        }
        fields = {key: value for key, value in values.items() if key in allowed}

        def operation() -> dict[str, Any]:
            with self.session() as session:
                preview = session.get(PreviewSession, preview_session_id)
                if preview is None:
                    raise KeyError(f"Preview session {preview_session_id} not found")
                for key, value in fields.items():
                    setattr(preview, key, _as_utc_datetime(value) if key in DATETIME_FIELDS else value)
                preview.updated_at = utc_datetime()
                session.flush()
                return self._model_dict(preview)

        return self._run(operation)

    def _preview_upsert_statement(self, values: list[dict[str, Any]]) -> Any:
        update_columns = {
            "description",
            "create_time",
            "cover_url",
            "share_url",
            "video_url",
            "content_type",
            "asset_count",
            "raw_json",
            "updated_at",
        }
        if self.dialect_name == "mysql":
            statement = mysql_insert(PreviewVideo).values(values)
            incoming = statement.inserted
            return statement.on_duplicate_key_update(
                **{
                    column: func.coalesce(getattr(incoming, column), getattr(PreviewVideo, column))
                    for column in update_columns
                }
            )
        if self.dialect_name == "sqlite":
            statement = sqlite_insert(PreviewVideo).values(values)
            incoming = statement.excluded
            return statement.on_conflict_do_update(
                index_elements=[PreviewVideo.preview_session_id, PreviewVideo.aweme_id],
                set_={
                    column: func.coalesce(getattr(incoming, column), getattr(PreviewVideo, column))
                    for column in update_columns
                },
            )
        raise RuntimeError(f"Unsupported database dialect: {self.dialect_name}")

    def bulk_upsert_preview_videos(
        self, preview_session_id: int, items: list[dict[str, Any]]
    ) -> list[int]:
        deduplicated = {str(item["aweme_id"]): item for item in items}
        if not deduplicated:
            return []

        def operation() -> list[int]:
            with self.session() as session:
                preview = session.get(PreviewSession, preview_session_id)
                if preview is None:
                    raise KeyError(f"Preview session {preview_session_id} not found")
                now = utc_datetime()
                values = []
                for item in deduplicated.values():
                    raw = item.get("raw")
                    values.append(
                        {
                            "preview_session_id": preview_session_id,
                            "aweme_id": str(item["aweme_id"]),
                            "description": item.get("description") or None,
                            "create_time": item.get("create_time") or None,
                            "cover_url": item.get("cover_url") or None,
                            "share_url": item.get("share_url") or None,
                            "video_url": item.get("video_url") or None,
                            "content_type": item.get("content_type") or "video",
                            "asset_count": max(1, int(item.get("asset_count") or 1)),
                            "raw_json": (
                                json.dumps(raw, ensure_ascii=False)
                                if raw not in (None, {}, [])
                                else None
                            ),
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                session.execute(self._preview_upsert_statement(values))
                ids = session.scalars(
                    select(PreviewVideo.id).where(
                        PreviewVideo.preview_session_id == preview_session_id,
                        PreviewVideo.aweme_id.in_(list(deduplicated)),
                    )
                ).all()
                preview.discovered_count = session.scalar(
                    select(func.count(PreviewVideo.id)).where(
                        PreviewVideo.preview_session_id == preview_session_id
                    )
                ) or 0
                preview.updated_at = now
                return [int(value) for value in ids]

        return self._run(operation)

    def list_preview_videos(
        self,
        token: str,
        *,
        page: int = 1,
        page_size: int = 30,
        keyword: str | None = None,
        content_type: str | None = None,
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        order = sort_order.lower()
        if order not in {"asc", "desc"}:
            raise ValueError("sort_order must be asc or desc")

        def operation() -> dict[str, Any]:
            with self.session() as session:
                preview = session.scalar(
                    select(PreviewSession).where(PreviewSession.token == token)
                )
                if preview is None:
                    raise KeyError(f"Preview session {token} not found")
                filters = [PreviewVideo.preview_session_id == preview.id]
                if keyword:
                    filters.append(PreviewVideo.description.like(f"%{keyword.strip()}%"))
                if content_type:
                    filters.append(PreviewVideo.content_type == content_type)
                total = int(
                    session.scalar(select(func.count(PreviewVideo.id)).where(*filters)) or 0
                )
                ordering = (
                    func.coalesce(PreviewVideo.create_time, 0).asc()
                    if order == "asc"
                    else func.coalesce(PreviewVideo.create_time, 0).desc()
                )
                videos = session.scalars(
                    select(PreviewVideo)
                    .where(*filters)
                    .order_by(ordering, PreviewVideo.id.desc() if order == "desc" else PreviewVideo.id.asc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).all()
                selection = self._preview_selection_state(session, preview)
                return {
                    "items": [
                        {
                            **self._model_dict(video),
                            "selected": self._preview_video_selected(video, preview),
                        }
                        for video in videos
                    ],
                    "total": total,
                    "total_pages": math.ceil(total / page_size) if total else 0,
                    "page": page,
                    "page_size": page_size,
                    "selection": selection,
                }

        return self._run(operation)

    def get_preview_video(self, token: str, preview_video_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                video = session.scalar(
                    select(PreviewVideo)
                    .join(PreviewSession, PreviewSession.id == PreviewVideo.preview_session_id)
                    .where(
                        PreviewSession.token == token,
                        PreviewVideo.id == preview_video_id,
                    )
                )
                if video is None:
                    raise KeyError(f"Preview video {preview_video_id} not found")
                return self._model_dict(video)

        return self._run(operation)

    @staticmethod
    def _preview_base_filter_clauses(
        preview_session_id: int, selection_filter: dict[str, Any] | None
    ) -> list[Any]:
        filters: list[Any] = [PreviewVideo.preview_session_id == preview_session_id]
        selection_filter = selection_filter or {}
        keyword = str(selection_filter.get("keyword") or "").strip()
        if keyword:
            filters.append(PreviewVideo.description.like(f"%{keyword}%"))
        content_type = selection_filter.get("content_type")
        if content_type in {"video", "images"}:
            filters.append(PreviewVideo.content_type == content_type)
        return filters

    @staticmethod
    def _preview_selection_range_clause(preview: PreviewSession) -> Any | None:
        selection_filter = preview.selection_filter or {}
        max_id = selection_filter.get("max_preview_id")
        auto_after = selection_filter.get("auto_select_after_id")
        clauses = []
        if max_id is not None:
            clauses.append(PreviewVideo.id <= int(max_id))
        if preview.auto_select_new:
            if auto_after is None:
                return None
            clauses.append(PreviewVideo.id > int(auto_after))
        if not clauses:
            return None
        return or_(*clauses)

    def _preview_selection_clauses(self, preview: PreviewSession) -> list[Any]:
        excluded = {str(value) for value in (preview.excluded_aweme_ids or [])}
        if preview.selection_mode == "explicit":
            selected = {str(value) for value in (preview.selected_aweme_ids or [])}
            clauses: list[Any] = [PreviewVideo.preview_session_id == preview.id]
            choices = []
            if selected:
                choices.append(PreviewVideo.aweme_id.in_(selected))
            auto_after = (preview.selection_filter or {}).get("auto_select_after_id")
            if preview.auto_select_new and auto_after is not None:
                choices.append(PreviewVideo.id > int(auto_after))
            if not choices:
                choices.append(PreviewVideo.id == -1)
            clauses.append(or_(*choices))
        else:
            clauses = self._preview_base_filter_clauses(
                preview.id,
                preview.selection_filter if preview.selection_mode == "filter" else None,
            )
            range_clause = self._preview_selection_range_clause(preview)
            if range_clause is not None:
                clauses.append(range_clause)
        if excluded:
            clauses.append(PreviewVideo.aweme_id.not_in(excluded))
        return clauses

    def _preview_selection_count(self, session: Session, preview: PreviewSession) -> int:
        clauses = self._preview_selection_clauses(preview)
        return int(session.scalar(select(func.count(PreviewVideo.id)).where(*clauses)) or 0)

    def _preview_selection_state(
        self, session: Session, preview: PreviewSession
    ) -> dict[str, Any]:
        return {
            "mode": preview.selection_mode,
            "auto_select_new": preview.auto_select_new,
            "selected_count": self._preview_selection_count(session, preview),
            "excluded_count": len(preview.excluded_aweme_ids or []),
            "filter": preview.selection_filter,
        }

    @staticmethod
    def _preview_video_selected(video: PreviewVideo, preview: PreviewSession) -> bool:
        aweme_id = str(video.aweme_id)
        if aweme_id in {str(value) for value in (preview.excluded_aweme_ids or [])}:
            return False
        selection_filter = preview.selection_filter or {}
        if preview.selection_mode == "explicit":
            if aweme_id in {str(value) for value in (preview.selected_aweme_ids or [])}:
                return True
            threshold = selection_filter.get("auto_select_after_id")
            return bool(
                preview.auto_select_new
                and threshold is not None
                and video.id > int(threshold)
            )
        if preview.selection_mode == "filter":
            keyword = str(selection_filter.get("keyword") or "").strip().lower()
            if keyword and keyword not in (video.description or "").lower():
                return False
            content_type = selection_filter.get("content_type")
            if content_type in {"video", "images"} and video.content_type != content_type:
                return False
        max_id = selection_filter.get("max_preview_id")
        auto_after = selection_filter.get("auto_select_after_id")
        if max_id is None and (not preview.auto_select_new or auto_after is None):
            return True
        return bool(
            (max_id is not None and video.id <= int(max_id))
            or (
                preview.auto_select_new
                and (auto_after is None or video.id > int(auto_after))
            )
        )

    def update_preview_selection(
        self,
        token: str,
        *,
        action: str,
        aweme_ids: list[str] | None = None,
        selection_filter: dict[str, Any] | None = None,
        auto_select_new: bool | None = None,
    ) -> dict[str, Any]:
        valid_actions = {"select", "deselect", "select_all", "clear_all", "select_filter", "set_auto"}
        if action not in valid_actions:
            raise ValueError(f"Unsupported selection action: {action}")

        def operation() -> dict[str, Any]:
            with self.session() as session:
                preview = session.scalar(
                    select(PreviewSession)
                    .where(PreviewSession.token == token)
                    .with_for_update()
                )
                if preview is None:
                    raise KeyError(f"Preview session {token} not found")
                max_id = int(
                    session.scalar(
                        select(func.max(PreviewVideo.id)).where(
                            PreviewVideo.preview_session_id == preview.id
                        )
                    )
                    or 0
                )
                selected = {str(value) for value in (preview.selected_aweme_ids or [])}
                excluded = {str(value) for value in (preview.excluded_aweme_ids or [])}
                requested = {str(value) for value in (aweme_ids or [])}
                if requested:
                    requested = set(
                        session.scalars(
                            select(PreviewVideo.aweme_id).where(
                                PreviewVideo.preview_session_id == preview.id,
                                PreviewVideo.aweme_id.in_(requested),
                            )
                        ).all()
                    )

                if action == "select":
                    excluded.difference_update(requested)
                    if preview.selection_mode == "explicit":
                        selected.update(requested)
                elif action == "deselect":
                    if preview.selection_mode == "explicit":
                        selected.difference_update(requested)
                    excluded.update(requested)
                elif action == "clear_all":
                    preview.selection_mode = "explicit"
                    selected.clear()
                    excluded.clear()
                    preview.selection_filter = None
                    preview.auto_select_new = False
                elif action == "select_all":
                    preview.selection_mode = "all"
                    selected.clear()
                    excluded.clear()
                    preview.auto_select_new = bool(auto_select_new)
                    preview.selection_filter = (
                        None
                        if preview.auto_select_new
                        else {"max_preview_id": max_id}
                    )
                elif action == "select_filter":
                    preview.selection_mode = "filter"
                    selected.clear()
                    excluded.clear()
                    preview.auto_select_new = bool(auto_select_new)
                    stored_filter = {
                        key: value
                        for key, value in (selection_filter or {}).items()
                        if key in {"keyword", "content_type"} and value not in (None, "")
                    }
                    if not preview.auto_select_new:
                        stored_filter["max_preview_id"] = max_id
                    preview.selection_filter = stored_filter
                elif action == "set_auto":
                    enabling = bool(auto_select_new)
                    stored_filter = dict(preview.selection_filter or {})
                    if enabling:
                        stored_filter["auto_select_after_id"] = max_id
                    else:
                        stored_filter.pop("auto_select_after_id", None)
                        if preview.selection_mode in {"all", "filter"}:
                            stored_filter["max_preview_id"] = max_id
                    preview.auto_select_new = enabling
                    preview.selection_filter = stored_filter or None

                preview.selected_aweme_ids = sorted(selected)
                preview.excluded_aweme_ids = sorted(excluded)
                preview.updated_at = utc_datetime()
                session.flush()
                return self._preview_selection_state(session, preview)

        return self._run(operation)

    def preview_confirmation_summary(
        self,
        token: str,
        *,
        download_policy: str,
        immediate_download_selected: bool,
    ) -> dict[str, Any]:
        policy = validate_download_policy(download_policy)

        def operation() -> dict[str, Any]:
            with self.session() as session:
                preview = session.scalar(
                    select(PreviewSession).where(PreviewSession.token == token)
                )
                if preview is None:
                    raise KeyError(f"Preview session {token} not found")
                selected_count = self._preview_selection_count(session, preview)
                create_jobs = should_download_selected_on_confirm(
                    policy, immediate_download_selected
                )
                return {
                    "selected_count": selected_count,
                    "estimated_download_jobs": selected_count if create_jobs else 0,
                    "download_policy": policy,
                    "immediate_download_selected": immediate_download_selected,
                }

        return self._run(operation)

    def confirm_preview_session(
        self,
        token: str,
        *,
        idempotency_key: str,
        download_policy: str,
        immediate_download_selected: bool,
        schedule_type: str,
        interval_value: int,
        daily_time: str | None,
        timezone_name: str,
        jitter_seconds: int = 0,
    ) -> dict[str, Any]:
        policy = validate_download_policy(download_policy)
        schedule_type, interval_value, daily_time, timezone_name = validate_schedule(
            schedule_type, interval_value, daily_time, timezone_name
        )
        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency_key is required")

        def operation() -> dict[str, Any]:
            with self._lock, self.session() as session:
                preview = session.scalar(
                    select(PreviewSession)
                    .where(PreviewSession.token == token)
                    .with_for_update()
                )
                if preview is None:
                    raise KeyError(f"Preview session {token} not found")
                identity_conditions = []
                if preview.sec_uid:
                    identity_conditions.append(Creator.sec_uid == preview.sec_uid)
                profile_url = preview.normalized_url or preview.submitted_url
                if profile_url:
                    identity_conditions.append(Creator.profile_url == profile_url)
                existing_creator = (
                    session.scalar(select(Creator).where(or_(*identity_conditions)).limit(1))
                    if identity_conditions
                    else None
                )
                if preview.status == "confirmed":
                    if preview.idempotency_key != key or existing_creator is None:
                        raise ValueError("Preview session has already been confirmed")
                    video_count = int(
                        session.scalar(
                            select(func.count(Video.id)).where(
                                Video.creator_id == existing_creator.id
                            )
                        )
                        or 0
                    )
                    job_count = int(
                        session.scalar(
                            select(func.count(DownloadJob.id)).where(
                                DownloadJob.creator_id == existing_creator.id
                            )
                        )
                        or 0
                    )
                    return {
                        "creator": self._model_dict(existing_creator),
                        "selected_count": video_count,
                        "download_jobs_created": job_count,
                        "idempotent_replay": True,
                    }
                if preview.status != "completed":
                    raise ValueError(f"Preview session cannot be confirmed from {preview.status}")
                if existing_creator is not None:
                    raise ValueError("This creator is already monitored")
                selected_count = self._preview_selection_count(session, preview)
                if selected_count < 1:
                    raise ValueError("Select at least one work before confirming")

                now = utc_datetime()
                next_run = calculate_next_run(
                    schedule_type=schedule_type,
                    interval_value=interval_value,
                    daily_time=daily_time,
                    timezone_name=timezone_name,
                    after=now.replace(tzinfo=timezone.utc),
                    jitter_seconds=jitter_seconds,
                )
                creator = Creator(
                    profile_url=profile_url,
                    sec_uid=preview.sec_uid,
                    nickname=preview.nickname,
                    avatar_url=preview.avatar_url,
                    enabled=True,
                    status="idle",
                    download_policy=policy,
                    policy_changed_at=now,
                    per_scan_limit=100,
                    interval_minutes=(
                        interval_value
                        if schedule_type == "minutes"
                        else interval_value * 60
                        if schedule_type == "hours"
                        else interval_value * 1440
                        if schedule_type == "days"
                        else 1440
                    ),
                    next_scan_at=next_run,
                )
                session.add(creator)
                session.flush()
                session.add(
                    CreatorSchedule(
                        creator_id=creator.id,
                        schedule_type=schedule_type,
                        interval_value=interval_value,
                        daily_time=daily_time,
                        timezone=timezone_name,
                        enabled=True,
                        next_run_at=next_run,
                        jitter_seconds=max(0, int(jitter_seconds)),
                    )
                )

                selected_videos = session.scalars(
                    select(PreviewVideo)
                    .where(*self._preview_selection_clauses(preview))
                    .order_by(PreviewVideo.id.asc())
                ).yield_per(200)
                create_jobs = should_download_selected_on_confirm(
                    policy, immediate_download_selected
                )
                job_count = 0
                latest_publish: int | None = None
                video_batch: list[Video] = []

                def flush_video_batch() -> None:
                    nonlocal job_count
                    if not video_batch:
                        return
                    session.flush()
                    if create_jobs:
                        for video in video_batch:
                            session.add(
                                DownloadJob(
                                    creator_id=creator.id,
                                    video_id=video.id,
                                    status="queued",
                                    priority=50,
                                    next_attempt_at=now,
                                )
                            )
                            job_count += 1
                    video_batch.clear()

                for preview_video in selected_videos:
                    video = Video(
                        creator_id=creator.id,
                        aweme_id=preview_video.aweme_id,
                        description=preview_video.description,
                        create_time=preview_video.create_time,
                        video_url=preview_video.video_url,
                        cover_url=preview_video.cover_url,
                        share_url=preview_video.share_url,
                        content_type=preview_video.content_type,
                        asset_count=preview_video.asset_count,
                        raw_json=preview_video.raw_json,
                        status="pending",
                        policy_snapshot=policy,
                        needs_confirmation=False,
                        discovered_at=now,
                    )
                    session.add(video)
                    video_batch.append(video)
                    if len(video_batch) >= 200:
                        flush_video_batch()
                    if preview_video.create_time:
                        latest_publish = max(
                            latest_publish or int(preview_video.create_time),
                            int(preview_video.create_time),
                        )
                flush_video_batch()
                creator.total_found = selected_count
                if latest_publish:
                    creator.latest_publish_at = datetime.fromtimestamp(
                        latest_publish, timezone.utc
                    ).replace(tzinfo=None)
                creator.updated_at = now
                preview.status = "confirmed"
                preview.confirmed_at = now
                preview.idempotency_key = key
                preview.updated_at = now
                session.flush()
                return {
                    "creator": self._model_dict(creator),
                    "selected_count": selected_count,
                    "download_jobs_created": job_count,
                    "idempotent_replay": False,
                }

        return self._run(operation)

    def cleanup_expired_preview_sessions(self, *, now: datetime | None = None) -> int:
        cutoff = now or utc_datetime()
        if cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)

        def operation() -> int:
            with self.session() as session:
                previews = session.scalars(
                    select(PreviewSession).where(
                        PreviewSession.expires_at <= cutoff,
                        PreviewSession.status != "confirmed",
                    )
                ).all()
                count = len(previews)
                for preview in previews:
                    session.delete(preview)
                return count

        return self._run(operation)

    def list_preview_aweme_ids(self, preview_session_id: int) -> list[str]:
        def operation() -> list[str]:
            with self.session() as session:
                values = session.scalars(
                    select(PreviewVideo.aweme_id)
                    .where(PreviewVideo.preview_session_id == preview_session_id)
                    .order_by(PreviewVideo.id.asc())
                ).all()
                return [str(value) for value in values]

        return self._run(operation)

    def get_creator(self, creator_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                creator = session.get(Creator, creator_id)
                if not creator:
                    raise KeyError(f"Creator {creator_id} not found")
                return self._model_dict(creator)

        return self._run(operation)

    def prepare_creator_deletion(self, creator_id: int) -> dict[str, Any]:
        """Disable a creator and request all of its active work to stop."""

        def operation() -> dict[str, Any]:
            with self._lock, self.session() as session:
                creator = session.scalar(
                    select(Creator).where(Creator.id == creator_id).with_for_update()
                )
                if creator is None:
                    raise KeyError(f"Creator {creator_id} not found")

                now = utc_datetime()
                creator.enabled = False
                creator.status = "deleting"
                creator.next_scan_at = None
                creator.updated_at = now
                schedule = session.scalar(
                    select(CreatorSchedule).where(
                        CreatorSchedule.creator_id == creator_id
                    )
                )
                if schedule is not None:
                    schedule.enabled = False
                    schedule.next_run_at = None
                    schedule.updated_at = now

                active_scan_jobs = 0
                for job in session.scalars(
                    select(ScanJob).where(
                        ScanJob.creator_id == creator_id,
                        ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES),
                    )
                ).all():
                    job.cancel_requested = True
                    job.pause_requested = False
                    if job.status in {"queued", "paused"}:
                        job.status = "cancelled"
                        job.ended_at = now
                    else:
                        job.status = "cancelling"
                        active_scan_jobs += 1
                    job.heartbeat_at = now
                    job.updated_at = now

                active_download_jobs = 0
                for job in session.scalars(
                    select(DownloadJob).where(
                        DownloadJob.creator_id == creator_id,
                        DownloadJob.status.in_(ACTIVE_DOWNLOAD_JOB_STATUSES),
                    )
                ).all():
                    job.cancel_requested = True
                    job.pause_requested = False
                    if job.status in {"queued", "paused"}:
                        job.status = "cancelled"
                        job.finished_at = now
                        job.locked_by = None
                        job.locked_at = None
                    else:
                        job.status = "cancelling"
                        active_download_jobs += 1
                    job.heartbeat_at = now
                    job.updated_at = now

                video_count, downloaded_count = session.execute(
                    select(
                        func.count(Video.id),
                        func.sum(case((Video.status == "downloaded", 1), else_=0)),
                    ).where(Video.creator_id == creator_id)
                ).one()
                paths = {
                    str(path)
                    for row in session.execute(
                        select(Video.file_path, Video.cover_path).where(
                            Video.creator_id == creator_id
                        )
                    ).all()
                    for path in row
                    if path
                }
                paths.update(
                    str(path)
                    for path in session.scalars(
                        select(VideoAsset.local_path)
                        .join(Video, Video.id == VideoAsset.video_id)
                        .where(Video.creator_id == creator_id, VideoAsset.local_path.is_not(None))
                    ).all()
                    if path
                )
                session.flush()
                return {
                    "creator": self._model_dict(creator),
                    "video_count": int(video_count or 0),
                    "downloaded_count": int(downloaded_count or 0),
                    "local_paths": sorted(paths),
                    "active_scan_jobs": active_scan_jobs,
                    "active_download_jobs": active_download_jobs,
                }

        return self._run(operation)

    def finalize_creator_scan_cancellation(self, creator_id: int) -> None:
        def operation() -> None:
            with self.session() as session:
                now = utc_datetime()
                jobs = session.scalars(
                    select(ScanJob).where(
                        ScanJob.creator_id == creator_id,
                        ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES),
                    )
                ).all()
                for job in jobs:
                    job.status = "cancelled"
                    job.cancel_requested = True
                    job.pause_requested = False
                    job.ended_at = now
                    job.heartbeat_at = now
                    job.updated_at = now

        self._run(operation)

    def delete_creator(self, creator_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                creator = session.get(Creator, creator_id)
                if creator is None:
                    raise KeyError(f"Creator {creator_id} not found")
                result = self._model_dict(creator)
                session.delete(creator)
                return result

        return self._run(operation)

    def find_creator_by_identity(
        self, *, profile_url: str | None = None, sec_uid: str | None = None
    ) -> dict[str, Any] | None:
        if not profile_url and not sec_uid:
            return None

        def operation() -> dict[str, Any] | None:
            with self.session() as session:
                conditions = []
                if profile_url:
                    conditions.append(Creator.profile_url == profile_url)
                if sec_uid:
                    conditions.append(Creator.sec_uid == sec_uid)
                creator = session.scalar(select(Creator).where(or_(*conditions)).limit(1))
                return self._model_dict(creator) if creator is not None else None

        return self._run(operation)

    def _creator_detail_dict(
        self,
        creator: Creator,
        schedule: CreatorSchedule | None,
        active_job: ScanJob | None,
    ) -> dict[str, Any]:
        result = self._model_dict(creator)
        result["schedule"] = self._model_dict(schedule) if schedule is not None else None
        result["active_scan_job"] = (
            self._model_dict(active_job) if active_job is not None else None
        )
        return result

    @staticmethod
    def _active_scan_job_ids():
        return (
            select(
                ScanJob.creator_id.label("creator_id"),
                func.max(ScanJob.id).label("job_id"),
            )
            .where(ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES))
            .group_by(ScanJob.creator_id)
            .subquery()
        )

    def get_creator_detail(self, creator_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                active_job_ids = self._active_scan_job_ids()
                row = session.execute(
                    select(Creator, CreatorSchedule, ScanJob)
                    .outerjoin(
                        CreatorSchedule, CreatorSchedule.creator_id == Creator.id
                    )
                    .outerjoin(
                        active_job_ids,
                        active_job_ids.c.creator_id == Creator.id,
                    )
                    .outerjoin(ScanJob, ScanJob.id == active_job_ids.c.job_id)
                    .where(Creator.id == creator_id)
                ).one_or_none()
                if row is None:
                    raise KeyError(f"Creator {creator_id} not found")
                return self._creator_detail_dict(*row)

        return self._run(operation)

    def list_creators(self) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self.session() as session:
                active_job_ids = self._active_scan_job_ids()
                rows = session.execute(
                    select(Creator, CreatorSchedule, ScanJob)
                    .outerjoin(
                        CreatorSchedule, CreatorSchedule.creator_id == Creator.id
                    )
                    .outerjoin(
                        active_job_ids,
                        active_job_ids.c.creator_id == Creator.id,
                    )
                    .outerjoin(ScanJob, ScanJob.id == active_job_ids.c.job_id)
                    .order_by(Creator.created_at.desc(), Creator.id.desc())
                ).all()
                return [self._creator_detail_dict(*row) for row in rows]

        return self._run(operation)

    def list_creators_page(
        self, *, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))

        def operation() -> dict[str, Any]:
            with self.session() as session:
                total = int(session.scalar(select(func.count()).select_from(Creator)) or 0)
                active_job_ids = self._active_scan_job_ids()
                rows = session.execute(
                    select(Creator, CreatorSchedule, ScanJob)
                    .outerjoin(
                        CreatorSchedule, CreatorSchedule.creator_id == Creator.id
                    )
                    .outerjoin(
                        active_job_ids,
                        active_job_ids.c.creator_id == Creator.id,
                    )
                    .outerjoin(ScanJob, ScanJob.id == active_job_ids.c.job_id)
                    .order_by(Creator.created_at.desc(), Creator.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).all()
                return {
                    "items": [self._creator_detail_dict(*row) for row in rows],
                    "total": total,
                    "total_pages": math.ceil(total / page_size) if total else 0,
                    "page": page,
                    "page_size": page_size,
                }

        return self._run(operation)

    def update_creator(self, creator_id: int, **values: Any) -> None:
        allowed = {
            "profile_url", "sec_uid", "nickname", "avatar_url", "enabled",
            "interval_minutes", "download_policy", "policy_changed_at", "per_scan_limit",
            "last_scan_at", "next_scan_at", "latest_publish_at", "total_found",
            "downloaded_count", "failed_count", "pending_confirmation_count", "status",
            "last_error",
        }
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return
        if "download_policy" in fields:
            fields["download_policy"] = validate_download_policy(str(fields["download_policy"]))
            fields.setdefault("policy_changed_at", utc_datetime())

        def operation() -> None:
            with self.session() as session:
                creator = session.get(Creator, creator_id)
                if not creator:
                    raise KeyError(f"Creator {creator_id} not found")
                for key, value in fields.items():
                    setattr(creator, key, _as_utc_datetime(value) if key in DATETIME_FIELDS else value)
                creator.updated_at = utc_datetime()

        self._run(operation)

    @staticmethod
    def _video_insert_values(
        creator_id: int, item: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        raw = item.get("raw")
        raw_json = None
        if raw not in (None, {}, []):
            raw_json = json.dumps(raw, ensure_ascii=False)
        return {
            "creator_id": creator_id,
            "aweme_id": str(item["aweme_id"]),
            "description": item.get("description") or None,
            "create_time": item.get("create_time") or None,
            "video_url": item.get("video_url") or None,
            "cover_url": item.get("cover_url") or None,
            "share_url": item.get("share_url") or None,
            "content_type": item.get("content_type") or "video",
            "asset_count": max(1, int(item.get("asset_count") or 1)),
            "is_daily": bool(item.get("is_daily")),
            "raw_json": raw_json,
            "remote_status": "active",
            "missing_count": 0,
            "last_seen_at": now,
            "updated_at": now,
        }

    def _video_upsert_statement(self, values: list[dict[str, Any]]) -> Any:
        if self.dialect_name == "mysql":
            statement = mysql_insert(Video).values(values)
            incoming = statement.inserted
            return statement.on_duplicate_key_update(
                description=func.coalesce(incoming.description, Video.description),
                create_time=func.coalesce(incoming.create_time, Video.create_time),
                video_url=func.coalesce(incoming.video_url, Video.video_url),
                cover_url=func.coalesce(incoming.cover_url, Video.cover_url),
                share_url=func.coalesce(incoming.share_url, Video.share_url),
                content_type=case(
                    (incoming.content_type != "video", incoming.content_type),
                    else_=Video.content_type,
                ),
                asset_count=case(
                    (incoming.asset_count > 1, incoming.asset_count), else_=Video.asset_count
                ),
                is_daily=case((incoming.is_daily.is_(True), True), else_=Video.is_daily),
                raw_json=func.coalesce(incoming.raw_json, Video.raw_json),
                remote_status="active",
                missing_count=0,
                last_seen_at=incoming.last_seen_at,
                updated_at=incoming.updated_at,
            )
        if self.dialect_name == "sqlite":
            statement = sqlite_insert(Video).values(values)
            incoming = statement.excluded
            return statement.on_conflict_do_update(
                index_elements=[Video.creator_id, Video.aweme_id],
                set_={
                    "description": func.coalesce(incoming.description, Video.description),
                    "create_time": func.coalesce(incoming.create_time, Video.create_time),
                    "video_url": func.coalesce(incoming.video_url, Video.video_url),
                    "cover_url": func.coalesce(incoming.cover_url, Video.cover_url),
                    "share_url": func.coalesce(incoming.share_url, Video.share_url),
                    "content_type": case(
                        (incoming.content_type != "video", incoming.content_type),
                        else_=Video.content_type,
                    ),
                    "asset_count": case(
                        (incoming.asset_count > 1, incoming.asset_count),
                        else_=Video.asset_count,
                    ),
                    "is_daily": case(
                        (incoming.is_daily.is_(True), True), else_=Video.is_daily
                    ),
                    "raw_json": func.coalesce(incoming.raw_json, Video.raw_json),
                    "remote_status": "active",
                    "missing_count": 0,
                    "last_seen_at": incoming.last_seen_at,
                    "updated_at": incoming.updated_at,
                },
            )
        raise RuntimeError(f"Unsupported database dialect: {self.dialect_name}")

    def bulk_upsert_videos(
        self, creator_id: int, items: list[dict[str, Any]]
    ) -> list[tuple[int, bool]]:
        deduplicated: dict[str, dict[str, Any]] = {}
        for item in items:
            deduplicated[str(item["aweme_id"])] = item
        if not deduplicated:
            return []

        def operation() -> list[tuple[int, bool]]:
            with self.session() as session:
                now = utc_datetime()
                aweme_ids = list(deduplicated)
                existing = dict(
                    session.execute(
                        select(Video.aweme_id, Video.id).where(
                            Video.creator_id == creator_id,
                            Video.aweme_id.in_(aweme_ids),
                        )
                    ).all()
                )
                values = [
                    self._video_insert_values(creator_id, item, now)
                    for item in deduplicated.values()
                ]
                session.execute(self._video_upsert_statement(values))
                rows = dict(
                    session.execute(
                        select(Video.aweme_id, Video.id).where(
                            Video.creator_id == creator_id,
                            Video.aweme_id.in_(aweme_ids),
                        )
                    ).all()
                )
                return [(int(rows[aweme_id]), aweme_id not in existing) for aweme_id in aweme_ids]

        return self._run(operation)

    def upsert_video(self, creator_id: int, item: dict[str, Any]) -> tuple[int, bool]:
        return self.bulk_upsert_videos(creator_id, [item])[0]

    def bulk_update_videos(self, updates: list[dict[str, Any]]) -> int:
        allowed = {
            "description", "create_time", "video_url", "cover_url", "share_url",
            "content_type", "asset_count", "is_daily", "status", "file_path",
            "cover_path", "file_size", "retry_count", "bytes_downloaded", "total_bytes",
            "last_error", "raw_json", "downloaded_at", "remote_status", "missing_count",
            "last_seen_at", "remote_changed_at", "policy_snapshot", "needs_confirmation",
        }
        mappings: list[dict[str, Any]] = []
        now = utc_datetime()
        for update in updates:
            if "id" not in update:
                raise ValueError("Each video update must include an id")
            mapping: dict[str, Any] = {"id": int(update["id"]), "updated_at": now}
            for key, value in update.items():
                if key in allowed:
                    mapping[key] = _as_utc_datetime(value) if key in DATETIME_FIELDS else value
            mappings.append(mapping)
        if not mappings:
            return 0

        def operation() -> int:
            with self.session() as session:
                session.bulk_update_mappings(Video, mappings)
                return len(mappings)

        return self._run(operation)

    def get_video(self, video_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                video = session.get(Video, video_id)
                if not video:
                    raise KeyError(f"Video {video_id} not found")
                return self._model_dict(video)

        return self._run(operation)

    def get_videos(self, video_ids: list[int]) -> list[dict[str, Any]]:
        unique_ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        if not unique_ids:
            return []

        def operation() -> list[dict[str, Any]]:
            with self.session() as session:
                videos = {
                    int(video.id): self._model_dict(video)
                    for video in session.scalars(
                        select(Video).where(Video.id.in_(unique_ids))
                    ).all()
                }
                missing = sorted(set(unique_ids) - set(videos))
                if missing:
                    raise KeyError(f"Videos not found: {missing}")
                return [videos[video_id] for video_id in unique_ids]

        return self._run(operation)

    def delete_video(self, video_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                video = session.get(Video, video_id)
                if video is None:
                    raise KeyError(f"Video {video_id} not found")
                result = self._model_dict(video)
                session.delete(video)
                return result

        return self._run(operation)

    @staticmethod
    def _local_file_state(file_path: str | None) -> tuple[str, bool]:
        if not file_path:
            return "not_downloaded", False
        try:
            exists = Path(file_path).exists()
        except OSError:
            exists = False
        return ("available" if exists else "missing"), exists

    @staticmethod
    def _video_filters(
        *,
        creator_id: int | None = None,
        status: str | None = None,
        content_type: str | None = None,
        keyword: str | None = None,
        needs_confirmation: bool | None = None,
    ) -> list[Any]:
        filters: list[Any] = []
        if creator_id is not None:
            filters.append(Video.creator_id == creator_id)
        if status:
            filters.append(Video.status == status)
        if content_type:
            filters.append(Video.content_type == content_type)
        if needs_confirmation is not None:
            filters.append(Video.needs_confirmation.is_(needs_confirmation))
        keyword = str(keyword or "").strip()
        if keyword:
            pattern = f"%{keyword}%"
            filters.append(or_(Video.description.like(pattern), Video.aweme_id.like(pattern)))
        return filters

    def _video_summary_dict(
        self, video: Video, creator_nickname: str | None
    ) -> dict[str, Any]:
        item = self._model_dict(video)
        item.pop("video_url", None)
        item.pop("raw_json", None)
        item["creator_nickname"] = creator_nickname
        file_state, file_exists = self._local_file_state(video.file_path)
        item["local_file_status"] = file_state
        item["local_file_exists"] = file_exists
        return item

    def list_videos(
        self, creator_id: int | None = None, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self.session() as session:
                statement = select(Video, Creator.nickname.label("creator_nickname")).join(Creator)
                if creator_id is not None:
                    statement = statement.where(Video.creator_id == creator_id)
                if status:
                    statement = statement.where(Video.status == status)
                statement = statement.order_by(func.coalesce(Video.create_time, 0).desc(), Video.id.desc())
                rows = session.execute(statement.limit(max(1, min(limit, 2000)))).all()
                return [self._video_summary_dict(video, nickname) for video, nickname in rows]

        return self._run(operation)

    def list_videos_page(
        self,
        *,
        creator_id: int | None = None,
        status: str | None = None,
        content_type: str | None = None,
        keyword: str | None = None,
        needs_confirmation: bool | None = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 30,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        sort = str(sort).strip().lower()
        if sort not in {"newest", "oldest"}:
            raise ValueError("sort must be newest or oldest")
        cursor_values: tuple[int, int] | None = None
        if cursor:
            try:
                padding = "=" * (-len(cursor) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
                cursor_values = (int(decoded[0]), int(decoded[1]))
            except (ValueError, TypeError, IndexError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
                raise ValueError("invalid video pagination cursor") from exc
        def operation() -> dict[str, Any]:
            with self.session() as session:
                filters = self._video_filters(
                    creator_id=creator_id,
                    status=status,
                    content_type=content_type,
                    keyword=keyword,
                    needs_confirmation=needs_confirmation,
                )

                total_statement = select(func.count()).select_from(Video)
                if filters:
                    total_statement = total_statement.where(*filters)
                total = int(session.scalar(total_statement) or 0)

                publish_order = func.coalesce(Video.create_time, 0)
                order_by = (
                    (publish_order.asc(), Video.id.asc())
                    if sort == "oldest"
                    else (publish_order.desc(), Video.id.desc())
                )
                statement = (
                    select(Video, Creator.nickname.label("creator_nickname"))
                    .join(Creator)
                    .order_by(*order_by)
                    .limit(page_size + 1)
                )
                if filters:
                    statement = statement.where(*filters)
                if cursor_values is not None:
                    cursor_time, cursor_id = cursor_values
                    cursor_filter = (
                        or_(publish_order > cursor_time, and_(publish_order == cursor_time, Video.id > cursor_id))
                        if sort == "oldest"
                        else or_(publish_order < cursor_time, and_(publish_order == cursor_time, Video.id < cursor_id))
                    )
                    statement = statement.where(cursor_filter)
                else:
                    statement = statement.offset((page - 1) * page_size)
                rows = session.execute(statement).all()
                has_more = len(rows) > page_size
                rows = rows[:page_size]
                items = [
                    self._video_summary_dict(video, nickname)
                    for video, nickname in rows
                ]
                next_cursor = None
                if has_more and rows:
                    last_video = rows[-1][0]
                    payload = json.dumps([int(last_video.create_time or 0), int(last_video.id)], separators=(",", ":"))
                    next_cursor = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
                return {
                    "items": items,
                    "total": total,
                    "total_pages": math.ceil(total / page_size) if total else 0,
                    "page": page,
                    "page_size": page_size,
                    "next_cursor": next_cursor,
                    "pagination_mode": "cursor" if cursor_values is not None else "page",
                }

        return self._run(operation)

    def get_video_playback_context(
        self,
        video_id: int,
        *,
        creator_id: int | None = None,
        status: str | None = None,
        content_type: str | None = None,
        keyword: str | None = None,
        sort: str = "newest",
    ) -> dict[str, Any]:
        sort = str(sort).strip().lower()
        if sort not in {"newest", "oldest"}:
            raise ValueError("sort must be newest or oldest")

        def operation() -> dict[str, Any]:
            with self.session() as session:
                filters = self._video_filters(
                    creator_id=creator_id,
                    status=status,
                    content_type=content_type,
                    keyword=keyword,
                )
                current_row = session.execute(
                    select(Video, Creator.nickname.label("creator_nickname"))
                    .join(Creator)
                    .where(Video.id == video_id, *filters)
                ).one_or_none()
                if current_row is None:
                    raise KeyError(f"Video {video_id} not found in playback filter")
                current, current_nickname = current_row
                publish_order = func.coalesce(Video.create_time, 0)
                current_publish = int(current.create_time or 0)
                if sort == "newest":
                    previous_clause = or_(
                        publish_order > current_publish,
                        and_(publish_order == current_publish, Video.id > current.id),
                    )
                    next_clause = or_(
                        publish_order < current_publish,
                        and_(publish_order == current_publish, Video.id < current.id),
                    )
                    previous_order = (publish_order.asc(), Video.id.asc())
                    next_order = (publish_order.desc(), Video.id.desc())
                else:
                    previous_clause = or_(
                        publish_order < current_publish,
                        and_(publish_order == current_publish, Video.id < current.id),
                    )
                    next_clause = or_(
                        publish_order > current_publish,
                        and_(publish_order == current_publish, Video.id > current.id),
                    )
                    previous_order = (publish_order.desc(), Video.id.desc())
                    next_order = (publish_order.asc(), Video.id.asc())

                def neighbor(clause: Any, ordering: tuple[Any, Any]) -> dict[str, Any] | None:
                    row = session.execute(
                        select(Video, Creator.nickname.label("creator_nickname"))
                        .join(Creator)
                        .where(*filters, clause)
                        .order_by(*ordering)
                        .limit(1)
                    ).one_or_none()
                    return self._video_summary_dict(*row) if row is not None else None

                total_statement = select(func.count()).select_from(Video)
                if filters:
                    total_statement = total_statement.where(*filters)
                position_statement = select(func.count()).select_from(Video).where(
                    *filters, previous_clause
                )
                return {
                    "current": self._video_summary_dict(current, current_nickname),
                    "previous": neighbor(previous_clause, previous_order),
                    "next": neighbor(next_clause, next_order),
                    "position": int(session.scalar(position_statement) or 0) + 1,
                    "total": int(session.scalar(total_statement) or 0),
                    "sort": sort,
                }

        return self._run(operation)

    def list_video_aweme_ids(self, creator_id: int) -> list[str]:
        def operation() -> list[str]:
            with self.session() as session:
                values = session.scalars(
                    select(Video.aweme_id)
                    .where(Video.creator_id == creator_id)
                    .order_by(Video.id.asc())
                ).all()
                return [str(value) for value in values]

        return self._run(operation)

    def enqueue_download_jobs(
        self,
        creator_id: int,
        video_ids: list[int],
        *,
        priority: int = 0,
        max_attempts: int = 5,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        unique_ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        if not unique_ids:
            return []

        def operation() -> list[dict[str, Any]]:
            with self._lock, self.session() as session:
                videos = {
                    int(video.id): video
                    for video in session.scalars(
                        select(Video)
                        .where(Video.id.in_(unique_ids), Video.creator_id == creator_id)
                        .with_for_update()
                    ).all()
                }
                if len(videos) != len(unique_ids):
                    missing = sorted(set(unique_ids) - set(videos))
                    raise KeyError(f"Videos not found for creator {creator_id}: {missing}")
                existing = {
                    int(job.video_id): job
                    for job in session.scalars(
                        select(DownloadJob)
                        .where(DownloadJob.video_id.in_(unique_ids))
                        .with_for_update()
                    ).all()
                }
                jobs: list[DownloadJob] = []
                now = utc_datetime()
                for video_id in unique_ids:
                    job = existing.get(video_id)
                    if job is None:
                        job = DownloadJob(
                            creator_id=creator_id,
                            video_id=video_id,
                            status="queued",
                            priority=priority,
                            max_attempts=max(1, max_attempts),
                            next_attempt_at=now,
                        )
                        session.add(job)
                    elif force and job.status not in {"queued", "running"}:
                        job.status = "queued"
                        job.priority = max(job.priority, priority)
                        job.attempts = 0
                        job.max_attempts = max(1, max_attempts)
                        job.next_attempt_at = now
                        job.pause_requested = False
                        job.cancel_requested = False
                        job.failure_reason = None
                        job.finished_at = None
                    else:
                        job.priority = max(job.priority, priority)
                    job.updated_at = now
                    jobs.append(job)
                session.flush()
                return [self._model_dict(job) for job in jobs]

        return self._run(operation)

    def get_download_job(self, job_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                row = session.execute(
                    select(DownloadJob, Video, Creator.nickname.label("creator_nickname"))
                    .join(Video, Video.id == DownloadJob.video_id)
                    .join(Creator, Creator.id == DownloadJob.creator_id)
                    .where(DownloadJob.id == job_id)
                ).one_or_none()
                if row is None:
                    raise KeyError(f"Download job {job_id} not found")
                job, video, nickname = row
                return self._download_job_dict(job, video, nickname)

        return self._run(operation)

    def _download_job_dict(
        self, job: DownloadJob, video: Video, creator_nickname: str | None
    ) -> dict[str, Any]:
        result = self._model_dict(job)
        result.update(
            {
                "aweme_id": video.aweme_id,
                "description": video.description,
                "content_type": video.content_type,
                "video_status": video.status,
                "file_path": video.file_path,
                "file_size": video.file_size,
                "creator_nickname": creator_nickname,
            }
        )
        file_state, file_exists = self._local_file_state(video.file_path)
        result["local_file_status"] = file_state
        result["local_file_exists"] = file_exists
        return result

    def list_download_jobs(
        self,
        *,
        creator_id: int | None = None,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self.session() as session:
                statement = (
                    select(DownloadJob, Video, Creator.nickname.label("creator_nickname"))
                    .join(Video, Video.id == DownloadJob.video_id)
                    .join(Creator, Creator.id == DownloadJob.creator_id)
                )
                if creator_id is not None:
                    statement = statement.where(DownloadJob.creator_id == creator_id)
                if statuses:
                    statement = statement.where(DownloadJob.status.in_(statuses))
                rows = session.execute(
                    statement.order_by(
                        DownloadJob.priority.desc(),
                        DownloadJob.created_at.asc(),
                        DownloadJob.id.asc(),
                    ).limit(max(1, min(limit, 1000)))
                ).all()
                return [
                    self._download_job_dict(job, video, nickname)
                    for job, video, nickname in rows
                ]

        return self._run(operation)

    def list_download_jobs_page(
        self,
        *,
        creator_id: int | None = None,
        statuses: set[str] | None = None,
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))

        def operation() -> dict[str, Any]:
            with self.session() as session:
                filters: list[Any] = []
                if creator_id is not None:
                    filters.append(DownloadJob.creator_id == creator_id)
                if statuses:
                    filters.append(DownloadJob.status.in_(statuses))

                total_statement = select(func.count()).select_from(DownloadJob)
                if filters:
                    total_statement = total_statement.where(*filters)
                total = int(session.scalar(total_statement) or 0)

                statement = (
                    select(DownloadJob, Video, Creator.nickname.label("creator_nickname"))
                    .join(Video, Video.id == DownloadJob.video_id)
                    .join(Creator, Creator.id == DownloadJob.creator_id)
                )
                if filters:
                    statement = statement.where(*filters)
                rows = session.execute(
                    statement.order_by(
                        DownloadJob.priority.desc(),
                        DownloadJob.created_at.asc(),
                        DownloadJob.id.asc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).all()
                return {
                    "items": [
                        self._download_job_dict(job, video, nickname)
                        for job, video, nickname in rows
                    ],
                    "total": total,
                    "total_pages": math.ceil(total / page_size) if total else 0,
                    "page": page,
                    "page_size": page_size,
                }

        return self._run(operation)

    def claim_download_jobs(
        self, worker_id: str, *, limit: int = 1
    ) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._lock, self.session() as session:
                now = utc_datetime()
                jobs = session.scalars(
                    select(DownloadJob)
                    .where(
                        DownloadJob.status == "queued",
                        DownloadJob.pause_requested.is_(False),
                        DownloadJob.cancel_requested.is_(False),
                        (DownloadJob.next_attempt_at.is_(None))
                        | (DownloadJob.next_attempt_at <= now),
                    )
                    .order_by(
                        DownloadJob.priority.desc(),
                        DownloadJob.created_at.asc(),
                        DownloadJob.id.asc(),
                    )
                    .limit(max(1, min(limit, 10)))
                    .with_for_update(skip_locked=True)
                ).all()
                for job in jobs:
                    job.status = "running"
                    job.locked_by = worker_id
                    job.locked_at = now
                    job.heartbeat_at = now
                    job.started_at = job.started_at or now
                    job.finished_at = None
                    job.attempts += 1
                    job.updated_at = now
                session.flush()
                return [self._model_dict(job) for job in jobs]

        return self._run(operation)

    def update_download_job(self, job_id: int, **values: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "priority",
            "attempts",
            "max_attempts",
            "next_attempt_at",
            "locked_by",
            "locked_at",
            "heartbeat_at",
            "pause_requested",
            "cancel_requested",
            "bytes_downloaded",
            "total_bytes",
            "speed_bytes_per_second",
            "temp_path",
            "failure_reason",
            "started_at",
            "finished_at",
        }
        fields = {key: value for key, value in values.items() if key in allowed}

        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.get(DownloadJob, job_id)
                if job is None:
                    raise KeyError(f"Download job {job_id} not found")
                for key, value in fields.items():
                    setattr(job, key, _as_utc_datetime(value) if key in DATETIME_FIELDS else value)
                job.updated_at = utc_datetime()
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def request_download_job_pause(self, job_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.scalar(
                    select(DownloadJob).where(DownloadJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise KeyError(f"Download job {job_id} not found")
                if job.status not in TERMINAL_DOWNLOAD_JOB_STATUSES:
                    job.pause_requested = True
                    job.status = "paused" if job.status == "queued" else "pausing"
                    job.heartbeat_at = utc_datetime()
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def request_download_job_cancel(self, job_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.scalar(
                    select(DownloadJob).where(DownloadJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise KeyError(f"Download job {job_id} not found")
                if job.status not in TERMINAL_DOWNLOAD_JOB_STATUSES:
                    now = utc_datetime()
                    job.cancel_requested = True
                    if job.status in {"queued", "paused"}:
                        job.status = "cancelled"
                        job.finished_at = now
                        job.locked_by = None
                        job.locked_at = None
                    else:
                        job.status = "cancelling"
                    job.heartbeat_at = now
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def resume_download_job(self, job_id: int) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.scalar(
                    select(DownloadJob).where(DownloadJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise KeyError(f"Download job {job_id} not found")
                if job.status != "paused":
                    raise ValueError(f"Download job {job_id} cannot resume from {job.status}")
                job.status = "queued"
                job.pause_requested = False
                job.cancel_requested = False
                job.next_attempt_at = utc_datetime()
                job.updated_at = utc_datetime()
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def retry_download_job(self, job_id: int, *, priority: int | None = None) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self.session() as session:
                job = session.scalar(
                    select(DownloadJob).where(DownloadJob.id == job_id).with_for_update()
                )
                if job is None:
                    raise KeyError(f"Download job {job_id} not found")
                if job.status not in {"failed", "cancelled"}:
                    raise ValueError(f"Download job {job_id} cannot retry from {job.status}")
                job.status = "queued"
                job.attempts = 0
                job.next_attempt_at = utc_datetime()
                job.locked_by = None
                job.locked_at = None
                job.pause_requested = False
                job.cancel_requested = False
                job.failure_reason = None
                job.finished_at = None
                if priority is not None:
                    job.priority = priority
                job.updated_at = utc_datetime()
                session.flush()
                return self._model_dict(job)

        return self._run(operation)

    def update_video(self, video_id: int, **values: Any) -> None:
        allowed = {
            "description", "create_time", "video_url", "cover_url", "share_url",
            "content_type", "asset_count", "is_daily", "status", "file_path",
            "cover_path", "file_size", "retry_count", "bytes_downloaded", "total_bytes",
            "last_error", "raw_json", "downloaded_at", "remote_status", "missing_count",
            "last_seen_at", "remote_changed_at", "policy_snapshot", "needs_confirmation",
        }
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return

        def operation() -> None:
            with self.session() as session:
                video = session.get(Video, video_id)
                if not video:
                    raise KeyError(f"Video {video_id} not found")
                for key, value in fields.items():
                    setattr(video, key, _as_utc_datetime(value) if key in DATETIME_FIELDS else value)
                video.updated_at = utc_datetime()

        self._run(operation)

    def recount_creator(self, creator_id: int) -> None:
        def operation() -> None:
            with self.session() as session:
                total, downloaded, failed, pending_confirmation = session.execute(
                    select(
                        func.count(Video.id),
                        func.sum(case((Video.status == "downloaded", 1), else_=0)),
                        func.sum(case((Video.status == "failed", 1), else_=0)),
                        func.sum(case((Video.needs_confirmation.is_(True), 1), else_=0)),
                    ).where(Video.creator_id == creator_id)
                ).one()
                creator = session.get(Creator, creator_id)
                if creator:
                    creator.total_found = int(total or 0)
                    creator.downloaded_count = int(downloaded or 0)
                    creator.failed_count = int(failed or 0)
                    creator.pending_confirmation_count = int(pending_confirmation or 0)
                    creator.updated_at = utc_datetime()

        self._run(operation)

    def reconcile_video_presence(
        self,
        creator_id: int,
        seen_aweme_ids: list[str],
        confirmation_scans: int = 2,
    ) -> list[dict[str, Any]]:
        seen = {str(aweme_id) for aweme_id in seen_aweme_ids}
        threshold = max(1, confirmation_scans)

        def operation() -> list[dict[str, Any]]:
            transitioning: list[dict[str, Any]] = []
            now = utc_datetime()
            with self.session() as session:
                videos = session.scalars(select(Video).where(Video.creator_id == creator_id)).all()
                for video in videos:
                    if video.aweme_id in seen:
                        if video.remote_status != "active":
                            video.remote_changed_at = now
                        video.remote_status = "active"
                        video.missing_count = 0
                        video.last_seen_at = now
                    else:
                        next_count = video.missing_count + 1
                        if video.remote_status != "removed_or_private" and next_count >= threshold:
                            transitioning.append(self._model_dict(video))
                            video.remote_changed_at = now
                        video.missing_count = next_count
                        video.remote_status = (
                            "removed_or_private" if next_count >= threshold else "unconfirmed_missing"
                        )
                    video.updated_at = now
            return transitioning

        return self._run(operation)

    def add_log(
        self,
        level: str,
        message: str,
        creator_id: int | None = None,
        video_id: int | None = None,
    ) -> None:
        def operation() -> None:
            with self.session() as session:
                session.add(
                    EventLog(
                        level=level,
                        message=message,
                        creator_id=creator_id,
                        video_id=video_id,
                    )
                )

        self._run(operation)

    def list_logs(self, limit: int = 200) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self.session() as session:
                logs = session.scalars(
                    select(EventLog).order_by(EventLog.id.desc()).limit(max(1, min(limit, 1000)))
                ).all()
                return [self._model_dict(log) for log in logs]

        return self._run(operation)

    def list_logs_page(
        self,
        *,
        level: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))

        def operation() -> dict[str, Any]:
            with self.session() as session:
                filters = [EventLog.level == level] if level else []
                total_statement = select(func.count()).select_from(EventLog)
                if filters:
                    total_statement = total_statement.where(*filters)
                total = int(session.scalar(total_statement) or 0)
                statement = select(EventLog)
                if filters:
                    statement = statement.where(*filters)
                logs = session.scalars(
                    statement.order_by(EventLog.created_at.desc(), EventLog.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).all()
                return {
                    "items": [self._model_dict(log) for log in logs],
                    "total": total,
                    "total_pages": math.ceil(total / page_size) if total else 0,
                    "page": page,
                    "page_size": page_size,
                }

        return self._run(operation)

    def get_setting(self, key: str) -> str | None:
        def operation() -> str | None:
            with self.session() as session:
                setting = session.get(AppSetting, key)
                return setting.value if setting else None

        return self._run(operation)

    def set_setting(self, key: str, value: str) -> None:
        def operation() -> None:
            with self.session() as session:
                setting = session.get(AppSetting, key)
                if setting is None:
                    session.add(AppSetting(key=key, value=value))
                else:
                    setting.value = value
                    setting.updated_at = utc_datetime()

        self._run(operation)

    def close(self) -> None:
        self.engine.dispose()

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_datetime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_datetime, onupdate=utc_datetime, nullable=False)


class Creator(TimestampMixin, Base):
    __tablename__ = "creators"
    __table_args__ = (
        Index("idx_creators_enabled_next_scan", "enabled", "next_scan_at"),
        Index("idx_creators_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Keep the unique key below MySQL/InnoDB's utf8mb4 index byte limit.
    profile_url: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    sec_uid: Mapped[str | None] = mapped_column(String(255), index=True)
    nickname: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="idle", nullable=False)
    download_policy: Mapped[str] = mapped_column(String(64), default="metadata_only", nullable=False)
    policy_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    per_scan_limit: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime)
    latest_publish_at: Mapped[datetime | None] = mapped_column(DateTime)
    total_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloaded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_confirmation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    schedule: Mapped["CreatorSchedule | None"] = relationship(back_populates="creator", cascade="all, delete-orphan", uselist=False)
    videos: Mapped[list["Video"]] = relationship(back_populates="creator", cascade="all, delete-orphan")


class CreatorSchedule(TimestampMixin, Base):
    __tablename__ = "creator_schedules"
    __table_args__ = (Index("idx_schedules_due", "enabled", "next_run_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id", ondelete="CASCADE"), unique=True, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(24), default="minutes", nullable=False)
    interval_value: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    daily_time: Mapped[str | None] = mapped_column(String(8))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    jitter_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    creator: Mapped[Creator] = relationship(back_populates="schedule")


class Video(TimestampMixin, Base):
    __tablename__ = "videos"
    __table_args__ = (
        UniqueConstraint("creator_id", "aweme_id", name="uq_videos_creator_aweme"),
        Index("idx_videos_creator_status_created", "creator_id", "status", "created_at"),
        Index("idx_videos_creator_publish", "creator_id", "create_time", "id"),
        Index("idx_videos_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id", ondelete="CASCADE"), nullable=False)
    aweme_id: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[int | None] = mapped_column(BigInteger)
    video_url: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    share_url: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(24), default="video", nullable=False)
    asset_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_daily: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    cover_path: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    remote_status: Mapped[str] = mapped_column(String(40), default="active", nullable=False)
    missing_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    remote_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utc_datetime, nullable=False)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    policy_snapshot: Mapped[str | None] = mapped_column(String(64))
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    creator: Mapped[Creator] = relationship(back_populates="videos")
    assets: Mapped[list["VideoAsset"]] = relationship(back_populates="video", cascade="all, delete-orphan")


class VideoAsset(TimestampMixin, Base):
    __tablename__ = "video_assets"
    __table_args__ = (
        UniqueConstraint("video_id", "position", name="uq_video_assets_position"),
        Index("idx_video_assets_video_type", "video_id", "asset_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(24), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    remote_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    video: Mapped[Video] = relationship(back_populates="assets")


class PreviewSession(TimestampMixin, Base):
    __tablename__ = "preview_sessions"
    __table_args__ = (Index("idx_preview_sessions_status_expires", "status", "expires_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    submitted_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str | None] = mapped_column(Text)
    sec_uid: Mapped[str | None] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    selection_mode: Mapped[str] = mapped_column(String(32), default="explicit", nullable=False)
    selection_filter: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    excluded_aweme_ids: Mapped[list[str] | None] = mapped_column(JSON)
    selected_aweme_ids: Mapped[list[str] | None] = mapped_column(JSON)
    auto_select_new: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    last_error: Mapped[str | None] = mapped_column(Text)


class PreviewVideo(TimestampMixin, Base):
    __tablename__ = "preview_videos"
    __table_args__ = (
        UniqueConstraint("preview_session_id", "aweme_id", name="uq_preview_video_aweme"),
        Index("idx_preview_videos_page", "preview_session_id", "create_time", "id"),
        Index("idx_preview_videos_type", "preview_session_id", "content_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preview_session_id: Mapped[int] = mapped_column(ForeignKey("preview_sessions.id", ondelete="CASCADE"), nullable=False)
    aweme_id: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[int | None] = mapped_column(BigInteger)
    cover_url: Mapped[str | None] = mapped_column(Text)
    share_url: Mapped[str | None] = mapped_column(Text)
    video_url: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(24), default="video", nullable=False)
    asset_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    raw_json: Mapped[str | None] = mapped_column(Text)


class ScanJob(TimestampMixin, Base):
    __tablename__ = "scan_jobs"
    __table_args__ = (
        Index("idx_scan_jobs_creator_status", "creator_id", "status"),
        Index("idx_scan_jobs_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("creators.id", ondelete="CASCADE"))
    preview_session_id: Mapped[int | None] = mapped_column(ForeignKey("preview_sessions.id", ondelete="CASCADE"))
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    scroll_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    written_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    item_limit: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    max_scrolls: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text)
    progress_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)


class DownloadJob(TimestampMixin, Base):
    __tablename__ = "download_jobs"
    __table_args__ = (
        UniqueConstraint("video_id", name="uq_download_jobs_video"),
        Index("idx_download_jobs_claim", "status", "priority", "created_at"),
        Index("idx_download_jobs_creator_status", "creator_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id", ondelete="CASCADE"), nullable=False)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    speed_bytes_per_second: Mapped[int | None] = mapped_column(BigInteger)
    temp_path: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class EventLog(Base):
    __tablename__ = "event_logs"
    __table_args__ = (
        Index("idx_event_logs_created", "created_at", "id"),
        Index("idx_event_logs_level_created", "level", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("creators.id", ondelete="SET NULL"))
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    job_type: Mapped[str | None] = mapped_column(String(32))
    job_id: Mapped[int | None] = mapped_column(Integer)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_datetime, nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(191), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_datetime, onupdate=utc_datetime, nullable=False)

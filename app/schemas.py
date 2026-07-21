from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


DownloadPolicy = Literal[
    "manual_selected_only",
    "selected_then_auto_new",
    "all_history_then_auto_new",
    "new_only_auto",
    "metadata_only",
    "new_pending_confirmation",
]


class CreatorCreate(BaseModel):
    profile_url: HttpUrl
    interval_minutes: int = Field(default=60, ge=5, le=10080)
    download_policy: DownloadPolicy = "metadata_only"


class CreatorUpdate(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    download_policy: DownloadPolicy | None = None
    per_scan_limit: int | None = Field(default=None, ge=20, le=1000)


class ContinueScanRequest(BaseModel):
    limit: int = Field(default=100, ge=20, le=1000)


class VideoIdsRequest(BaseModel):
    video_ids: list[int] = Field(min_length=1, max_length=100)


class PendingConfirmationRequest(VideoIdsRequest):
    action: Literal["download", "keep_metadata"]


class DeleteRequest(BaseModel):
    delete_local_files: bool = False


class PreviewCreate(BaseModel):
    profile_url: HttpUrl


class PreviewContinueRequest(BaseModel):
    limit: int = Field(default=100, ge=20, le=1000)


class PreviewSelectionUpdate(BaseModel):
    action: Literal[
        "select", "deselect", "select_all", "clear_all", "select_filter", "set_auto"
    ]
    aweme_ids: list[str] = Field(default_factory=list, max_length=100)
    filter: dict[str, str] | None = None
    auto_select_new: bool | None = None


class PreviewConfirmationOptions(BaseModel):
    download_policy: DownloadPolicy
    immediate_download_selected: bool = False
    schedule_type: Literal["minutes", "hours", "daily", "days"]
    interval_value: int = Field(default=1, ge=1, le=10080)
    daily_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
    timezone: str = "Asia/Shanghai"


class PreviewConfirmRequest(PreviewConfirmationOptions):
    idempotency_key: str = Field(min_length=8, max_length=128)


class CreatorScheduleUpdate(BaseModel):
    schedule_type: Literal["minutes", "hours", "daily", "days"]
    interval_value: int = Field(default=1, ge=1, le=10080)
    daily_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
    timezone: str = "Asia/Shanghai"
    enabled: bool = True


class SettingUpdate(BaseModel):
    download_dir: str | None = None


class DingTalkUpdate(BaseModel):
    enabled: bool
    webhook: str | None = None
    secret: str | None = None

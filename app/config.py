from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "抖音视频订阅下载器"
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = ROOT_DIR / "data"
    download_dir: Path = ROOT_DIR / "downloads"
    browser_data_dir: Path = ROOT_DIR / "browser_data"
    browser_headless: bool = False
    browser_channel: str | None = "chrome"
    browser_proxy: str | None = None
    browser_cdp_url: str | None = None
    linux_auto_browser: bool = True
    linux_chromium_no_sandbox: bool = False
    linux_display: str = ":99"
    linux_cdp_port: int = 9222
    linux_novnc_enabled: bool = True
    linux_novnc_mode: str = "on_demand"
    linux_novnc_idle_seconds: int = 120
    linux_vnc_port: int = 5900
    linux_novnc_port: int = 6080
    linux_novnc_bind_address: str = "127.0.0.1"
    linux_novnc_web_dir: Path = Path("/usr/share/novnc")
    linux_vnc_password: str | None = None
    linux_vnc_poll_ms: int = 80
    linux_vnc_defer_ms: int = 80
    default_interval_minutes: int = 60
    scan_poll_seconds: int = 30
    scan_scroll_wait_ms: int = 1300
    scan_stable_rounds: int = 7
    scan_batch_size: int = 30
    scan_no_progress_seconds: int = 90
    scan_continue_limit: int = 100
    preview_session_ttl_minutes: int = Field(default=120, ge=15, le=1440)
    max_scan_scrolls: int = 1000
    scan_max_runtime_seconds: int = 900
    schedule_jitter_ratio: float = 0.1
    schedule_jitter_seconds: int = Field(default=120, ge=0, le=600)
    download_concurrency: int = Field(default=1, ge=1, le=3)
    request_timeout_seconds: int = 90
    dingtalk_enabled: bool = False
    dingtalk_webhook: str | None = None
    dingtalk_secret: str | None = None
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "DOUYIN_DATABASE_URL"),
    )
    database_pool_size: int = Field(default=3, ge=1, le=20)
    database_max_overflow: int = Field(default=1, ge=0, le=20)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60)
    database_connect_retries: int = Field(default=3, ge=1, le=10)

    model_config = SettingsConfigDict(
        env_prefix="DOUYIN_",
        env_file=ROOT_DIR / ".env",
        extra="ignore",
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "douyin.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path.as_posix()}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.browser_data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()

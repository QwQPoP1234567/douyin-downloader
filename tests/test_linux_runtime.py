from pathlib import Path

import pytest

from app.config import Settings
from app.linux_runtime import prepare_linux_runtime


def test_linux_runtime_skips_when_external_cdp_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.linux_runtime.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
        browser_cdp_url="http://127.0.0.1:9222",
    )
    assert prepare_linux_runtime(settings) is None


def test_linux_without_display_requires_auto_browser_or_cdp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.linux_runtime.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
        linux_auto_browser=False,
    )
    with pytest.raises(RuntimeError, match="没有 DISPLAY"):
        prepare_linux_runtime(settings)

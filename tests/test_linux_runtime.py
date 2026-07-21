from pathlib import Path

import pytest

from app.config import Settings
from app.linux_runtime import LinuxRuntime, prepare_linux_runtime


def test_linux_runtime_can_disable_chromium_sandbox_for_restricted_containers(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
        linux_chromium_no_sandbox=True,
    )

    command = LinuxRuntime(settings)._build_chrome_command("chromium")

    assert command[:2] == ["chromium", "--no-sandbox"]


def test_linux_runtime_replaces_stale_log_on_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    settings.ensure_dirs()
    log_path = settings.data_dir / "linux-runtime.log"
    log_path.write_text("old sandbox failure", encoding="utf-8")
    runtime = LinuxRuntime(settings)
    monkeypatch.setattr(runtime, "_find_binary", lambda *names: None)

    with pytest.raises(RuntimeError, match="缺少"):
        runtime.start()
    runtime.stop()

    assert log_path.read_bytes() == b""


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

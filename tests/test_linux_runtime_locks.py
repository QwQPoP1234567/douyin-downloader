from pathlib import Path

from app.config import Settings
from app.linux_runtime import LinuxRuntime


def test_stale_chromium_locks_are_removed_without_touching_cookies(tmp_path: Path) -> None:
    browser = tmp_path / "browser"
    browser.mkdir()
    names = ("SingletonCookie", "SingletonLock", "SingletonSocket", "DevToolsActivePort")
    for name in names:
        (browser / name).write_text("stale", encoding="utf-8")
    cookies = browser / "Cookies"
    cookies.write_text("keep", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=browser,
    )

    LinuxRuntime(settings)._clear_stale_chromium_locks()

    assert cookies.read_text(encoding="utf-8") == "keep"
    assert not any((browser / name).exists() for name in names)

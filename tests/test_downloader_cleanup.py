import os
import time
from pathlib import Path

from app.config import Settings
from app.downloader import VideoDownloader


def test_cleanup_removes_only_stale_invalid_temp_files(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", download_dir=tmp_path / "downloads")
    settings.download_dir.mkdir(parents=True)
    downloader = VideoDownloader(None, settings)  # type: ignore[arg-type]
    orphan_part = settings.download_dir / "orphan.mp4.part"
    orphan_url = settings.download_dir / "missing.mp4.part.url"
    resumable_part = settings.download_dir / "resume.mp4.part"
    resumable_url = settings.download_dir / "resume.mp4.part.url"
    fresh_part = settings.download_dir / "fresh.mp4.part"
    for path in (orphan_part, orphan_url, resumable_part, resumable_url, fresh_part):
        path.write_text("data", encoding="utf-8")
    stale = time.time() - 7200
    for path in (orphan_part, orphan_url, resumable_part, resumable_url):
        os.utime(path, (stale, stale))

    removed = downloader.cleanup_stale_temp_files()

    assert removed == 2
    assert not orphan_part.exists()
    assert not orphan_url.exists()
    assert resumable_part.exists() and resumable_url.exists()
    assert fresh_part.exists()

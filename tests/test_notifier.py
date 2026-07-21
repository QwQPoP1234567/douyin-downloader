from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.config import Settings
from app.db import Database
from app.notifier import DingTalkConfigError, DingTalkNotifier


def make_notifier(tmp_path: Path) -> DingTalkNotifier:
    db = Database(tmp_path / "test.db")
    db.initialize()
    settings = Settings(
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "downloads",
        browser_data_dir=tmp_path / "browser",
    )
    return DingTalkNotifier(db, settings)


def test_dingtalk_signing_has_required_parameters(tmp_path: Path) -> None:
    notifier = make_notifier(tmp_path)
    url = notifier.signed_url(
        "https://oapi.dingtalk.com/robot/send?access_token=test-token",
        "SECtest-secret",
        timestamp=1_700_000_000_000,
    )
    query = parse_qs(urlparse(url).query)
    assert query["access_token"] == ["test-token"]
    assert query["timestamp"] == ["1700000000000"]
    assert len(query["sign"][0]) > 20


def test_configuration_masks_webhook_and_secret(tmp_path: Path) -> None:
    notifier = make_notifier(tmp_path)
    status = notifier.configure(
        True,
        "https://oapi.dingtalk.com/robot/send?access_token=private-token",
        "SECprivate-secret",
    )
    assert status["configured"] is True
    assert "private-token" not in status["webhook_masked"]
    assert "private-secret" not in str(status)


def test_rejects_non_dingtalk_webhook(tmp_path: Path) -> None:
    notifier = make_notifier(tmp_path)
    try:
        notifier.configure(True, "https://example.com/hook?access_token=x", "SECx")
    except DingTalkConfigError:
        pass
    else:
        raise AssertionError("必须拒绝非钉钉域名")

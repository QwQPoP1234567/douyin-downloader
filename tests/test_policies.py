from app.policies import should_auto_download, should_request_confirmation


def test_download_policy_behaviour() -> None:
    assert should_auto_download("all_history_then_auto_new", "continue") is True
    assert should_auto_download("new_only_auto", "incremental") is True
    assert should_auto_download("new_only_auto", "initial") is False
    assert should_auto_download("new_only_auto", "continue") is False
    assert should_auto_download("metadata_only", "incremental") is False
    assert should_request_confirmation("new_pending_confirmation", "incremental") is True
    assert should_request_confirmation("new_pending_confirmation", "initial") is False
    assert should_request_confirmation("new_pending_confirmation", "continue") is False

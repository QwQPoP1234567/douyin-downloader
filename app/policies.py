from __future__ import annotations


DOWNLOAD_POLICIES = {
    "manual_selected_only",
    "selected_then_auto_new",
    "all_history_then_auto_new",
    "new_only_auto",
    "metadata_only",
    "new_pending_confirmation",
}

AUTO_NEW_POLICIES = {
    "selected_then_auto_new",
    "all_history_then_auto_new",
    "new_only_auto",
}


def validate_download_policy(value: str) -> str:
    policy = value.strip().lower()
    if policy not in DOWNLOAD_POLICIES:
        raise ValueError(f"Unsupported download policy: {value}")
    return policy


def should_auto_download(policy: str, job_type: str) -> bool:
    policy = validate_download_policy(policy)
    if policy == "all_history_then_auto_new":
        return True
    return policy in {"selected_then_auto_new", "new_only_auto"} and job_type == "incremental"


def should_request_confirmation(policy: str, job_type: str) -> bool:
    return validate_download_policy(policy) == "new_pending_confirmation" and job_type == "incremental"


def should_download_selected_on_confirm(policy: str, immediate_selected: bool) -> bool:
    policy = validate_download_policy(policy)
    return bool(
        immediate_selected
        or policy in {"selected_then_auto_new", "all_history_then_auto_new"}
    )

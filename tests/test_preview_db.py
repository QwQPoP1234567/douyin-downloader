from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import Database


def test_preview_session_batches_and_database_pagination(tmp_path: Path) -> None:
    db = Database(tmp_path / "preview.db")
    db.initialize()
    preview = db.create_preview_session("https://v.douyin.com/test/")
    items = [
        {
            "aweme_id": str(index),
            "description": f"作品 {index}",
            "create_time": index,
            "content_type": "images" if index % 2 else "video",
            "cover_url": f"https://example.test/{index}.jpg",
        }
        for index in range(1, 66)
    ]

    ids = db.bulk_upsert_preview_videos(preview["id"], items + [items[0]])
    first_page = db.list_preview_videos(preview["token"], page=1, page_size=30)
    filtered = db.list_preview_videos(
        preview["token"],
        page=1,
        page_size=10,
        keyword="作品 1",
        content_type="images",
        sort_order="asc",
    )

    assert len(ids) == 65
    assert first_page["total"] == 65
    assert first_page["total_pages"] == 3
    assert len(first_page["items"]) == 30
    assert first_page["items"][0]["aweme_id"] == "65"
    assert all(item["content_type"] == "images" for item in filtered["items"])
    preview_video = db.get_preview_video(preview["token"], first_page["items"][0]["id"])
    assert preview_video["aweme_id"] == "65"
    assert db.get_preview_session(preview["token"])["discovered_count"] == 65
    db.close()


def test_expired_preview_sessions_are_deleted(tmp_path: Path) -> None:
    db = Database(tmp_path / "preview-expiry.db")
    db.initialize()
    preview = db.create_preview_session("https://v.douyin.com/expired/")
    db.update_preview_session(
        preview["id"],
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    deleted = db.cleanup_expired_preview_sessions()

    assert deleted == 1
    try:
        db.get_preview_session(preview["token"])
    except KeyError:
        pass
    else:
        raise AssertionError("expired preview session should be deleted")
    db.close()


def test_preview_selection_rules_work_across_pages_and_future_batches(tmp_path: Path) -> None:
    db = Database(tmp_path / "preview-selection.db")
    db.initialize()
    preview = db.create_preview_session("https://v.douyin.com/selection/")
    db.bulk_upsert_preview_videos(
        preview["id"],
        [
            {
                "aweme_id": str(index),
                "description": f"标题 {index}",
                "create_time": index,
                "content_type": "images" if index % 2 else "video",
            }
            for index in range(1, 61)
        ],
    )

    explicit = db.update_preview_selection(
        preview["token"], action="select", aweme_ids=["1", "31"]
    )
    assert explicit["selected_count"] == 2
    page_two = db.list_preview_videos(preview["token"], page=2, page_size=30)
    assert next(item for item in page_two["items"] if item["aweme_id"] == "1")["selected"] is True

    selected_all = db.update_preview_selection(
        preview["token"], action="select_all", auto_select_new=False
    )
    assert selected_all["selected_count"] == 60
    db.bulk_upsert_preview_videos(
        preview["id"], [{"aweme_id": "61", "description": "后发现 61", "create_time": 61}]
    )
    assert db.list_preview_videos(preview["token"])["selection"]["selected_count"] == 60

    db.update_preview_selection(
        preview["token"], action="set_auto", auto_select_new=True
    )
    db.bulk_upsert_preview_videos(
        preview["id"], [{"aweme_id": "62", "description": "后发现 62", "create_time": 62}]
    )
    auto_state = db.list_preview_videos(preview["token"])["selection"]
    assert auto_state["selected_count"] == 61
    db.update_preview_selection(preview["token"], action="deselect", aweme_ids=["62"])
    assert db.list_preview_videos(preview["token"])["selection"]["selected_count"] == 60

    filtered = db.update_preview_selection(
        preview["token"],
        action="select_filter",
        selection_filter={"content_type": "images", "keyword": "标题"},
        auto_select_new=False,
    )
    assert filtered["selected_count"] == 30
    db.close()


def test_preview_confirmation_is_atomic_and_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "preview-confirm.db")
    db.initialize()
    preview = db.create_preview_session("https://www.douyin.com/user/confirm")
    db.update_preview_session(
        preview["id"],
        normalized_url="https://www.douyin.com/user/confirm",
        sec_uid="sec-confirm",
        nickname="确认用户",
        status="completed",
    )
    db.bulk_upsert_preview_videos(
        preview["id"],
        [
            {"aweme_id": "1", "description": "作品1", "create_time": 100},
            {"aweme_id": "2", "description": "作品2", "create_time": 200},
            {"aweme_id": "3", "description": "作品3", "create_time": 300},
        ],
    )
    db.update_preview_selection(
        preview["token"], action="select_all", auto_select_new=True
    )

    summary = db.preview_confirmation_summary(
        preview["token"],
        download_policy="selected_then_auto_new",
        immediate_download_selected=False,
    )
    result = db.confirm_preview_session(
        preview["token"],
        idempotency_key="confirm-key-123",
        download_policy="selected_then_auto_new",
        immediate_download_selected=False,
        schedule_type="daily",
        interval_value=1,
        daily_time="09:30",
        timezone_name="Asia/Shanghai",
        jitter_seconds=10,
    )
    replay = db.confirm_preview_session(
        preview["token"],
        idempotency_key="confirm-key-123",
        download_policy="selected_then_auto_new",
        immediate_download_selected=False,
        schedule_type="daily",
        interval_value=1,
        daily_time="09:30",
        timezone_name="Asia/Shanghai",
        jitter_seconds=10,
    )

    creator_id = result["creator"]["id"]
    assert summary["selected_count"] == 3
    assert summary["estimated_download_jobs"] == 3
    assert result["selected_count"] == 3
    assert result["download_jobs_created"] == 3
    assert replay["idempotent_replay"] is True
    assert replay["creator"]["id"] == creator_id
    assert len(db.list_videos(creator_id=creator_id)) == 3
    assert len(db.list_download_jobs(creator_id=creator_id)) == 3
    assert db.get_creator_schedule(creator_id)["daily_time"] == "09:30:00"
    assert db.get_preview_session(preview["token"])["status"] == "confirmed"
    db.close()

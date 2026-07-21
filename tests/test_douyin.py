import asyncio

import json

from app.config import Settings
from app.douyin import (
    InvalidProfileUrl,
    ScanBatchAccumulator,
    filter_profile_items,
    find_aweme_items,
    parse_aweme,
    validate_profile_url,
)
from app.douyin import DouyinScanner


def sample_aweme() -> dict:
    return {
        "aweme_id": "7390000000000000000",
        "desc": "示例视频",
        "create_time": 1_720_000_000,
        "author": {
            "nickname": "测试用户",
            "sec_uid": "MS4wLjABAAAA",
            "avatar_thumb": {"url_list": ["https://cdn.test/avatar.jpg"]},
        },
        "video": {
            "bit_rate": [
                {"bit_rate": 1000, "play_addr": {"url_list": ["https://cdn.test/low.mp4"]}},
                {"bit_rate": 3000, "play_addr": {"url_list": ["https://cdn.test/high.mp4"]}},
            ],
            "origin_cover": {"url_list": ["https://cdn.test/cover.jpg"]},
        },
    }


def test_parse_aweme_prefers_high_bitrate() -> None:
    parsed = parse_aweme(sample_aweme())
    assert parsed is not None
    assert parsed["video_url"] == "https://cdn.test/high.mp4"
    assert parsed["nickname"] == "测试用户"
    assert parsed["sec_uid"] == "MS4wLjABAAAA"
    assert parsed["avatar_url"] == "https://cdn.test/avatar.jpg"


def test_parse_aweme_discards_unneeded_large_payload_fields() -> None:
    aweme = sample_aweme()
    aweme["unrelated_response_data"] = "x" * 200_000

    parsed = parse_aweme(aweme)

    assert parsed is not None
    assert "unrelated_response_data" not in parsed["raw"]
    assert len(json.dumps(parsed["raw"], ensure_ascii=False).encode("utf-8")) < 60_000
    assert parsed["raw"]["video"]["bit_rate"][0]["play_addr"]["url_list"]


def test_parse_aweme_prefers_resolution_before_bitrate() -> None:
    aweme = sample_aweme()
    aweme["video"]["bit_rate"] = [
        {
            "bit_rate": 8_000,
            "FPS": 30,
            "play_addr": {"height": 720, "width": 1280, "url_list": ["https://cdn.test/720.mp4"]},
        },
        {
            "bit_rate": 4_000,
            "FPS": 30,
            "play_addr": {"height": 1080, "width": 1920, "url_list": ["https://cdn.test/1080.mp4"]},
        },
    ]
    parsed = parse_aweme(aweme)
    assert parsed is not None
    assert parsed["video_url"] == "https://cdn.test/1080.mp4"


def test_parse_image_note_uses_original_images_not_soundtrack() -> None:
    aweme = sample_aweme()
    aweme["aweme_id"] = "7414761041587948800"
    aweme["aweme_type"] = 68
    aweme["video"] = {
        "duration": 0,
        "play_addr": {"url_list": ["https://cdn.test/soundtrack.mp3"]},
    }
    aweme["images"] = [
        {
            "url_list": ["https://cdn.test/original.webp"],
            "download_url_list": ["https://cdn.test/dy-water.webp"],
        }
    ]
    parsed = parse_aweme(aweme)
    assert parsed is not None
    assert parsed["content_type"] == "images"
    assert parsed["video_url"] is None
    assert parsed["image_urls"] == ["https://cdn.test/original.webp"]
    assert parsed["share_url"] == "https://www.douyin.com/note/7414761041587948800"


def test_find_aweme_items_in_nested_response() -> None:
    items = list(find_aweme_items({"data": {"aweme_list": [sample_aweme()]}}))
    assert [item["aweme_id"] for item in items] == ["7390000000000000000"]


def test_profile_url_validation() -> None:
    assert validate_profile_url("https://v.douyin.com/abc/") == "https://v.douyin.com/abc/"
    try:
        validate_profile_url("https://example.com/user/abc")
    except InvalidProfileUrl:
        pass
    else:
        raise AssertionError("非抖音域名应被拒绝")


class FakeRequest:
    async def all_headers(self):
        return {"user-agent": "test"}


class FakeRiskResponse:
    url = "https://www.douyin.com/aweme/v1/web/aweme/post/"
    status = 200
    request = FakeRequest()

    async def header_value(self, _name: str):
        return "application/json"

    async def json(self):
        return {"status_code": 5, "status_msg": "访问频繁，请完成验证"}


def test_interface_risk_message_is_detected(tmp_path) -> None:
    scanner = DouyinScanner(None, Settings(browser_data_dir=tmp_path / "browser"))  # type: ignore[arg-type]
    found = {}
    risks = []
    asyncio.run(scanner._capture_response(FakeRiskResponse(), found, risks))  # type: ignore[arg-type]
    assert risks == ["访问频繁，请完成验证"]


def test_profile_items_are_filtered_by_target_author() -> None:
    items = [
        {"aweme_id": "1", "sec_uid": "target"},
        {"aweme_id": "2", "sec_uid": "other"},
        {"aweme_id": "3", "sec_uid": None},
    ]
    assert [item["aweme_id"] for item in filter_profile_items(items, "target")] == ["1"]


def test_scan_batches_are_deduplicated_limited_and_released() -> None:
    batches: list[list[str]] = []

    async def run() -> ScanBatchAccumulator:
        async def on_batch(items: list[dict]) -> None:
            batches.append([str(item["aweme_id"]) for item in items])

        accumulator = ScanBatchAccumulator(
            item_limit=5,
            batch_size=2,
            on_batch=on_batch,
            ignored_ids={"1", "2"},
        )
        await accumulator.add(
            [
                {"aweme_id": "1", "create_time": 10},
                {"aweme_id": "2", "create_time": 20},
                {"aweme_id": "2", "create_time": 20},
                {"aweme_id": "3", "create_time": 15},
                {"aweme_id": "4", "create_time": 25},
                {"aweme_id": "5", "create_time": 5},
                {"aweme_id": "6", "create_time": 30},
                {"aweme_id": "7", "create_time": 35},
                {"aweme_id": "8", "create_time": 40},
            ]
        )
        await accumulator.flush()
        return accumulator

    accumulator = asyncio.run(run())

    assert batches == [["3", "4"], ["5", "6"], ["7"]]
    assert accumulator.aweme_ids == ["3", "4", "5", "6", "7"]
    assert accumulator.encountered_aweme_ids == ["1", "2", "3", "4", "5", "6", "7", "8"]
    assert accumulator.collected == []
    assert accumulator.latest_create_time == 35
    assert accumulator.reached_limit is True

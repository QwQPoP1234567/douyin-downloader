import json

from app.downloader import candidate_image_urls, candidate_video_urls, safe_name


def test_safe_name_removes_windows_invalid_characters() -> None:
    assert safe_name('a<b>:c/d\\e|f?g*"', "fallback") == "a_b__c_d_e_f_g"


def test_candidate_urls_keep_direct_first_and_deduplicate() -> None:
    video = {
        "video_url": "https://cdn.test/direct.mp4",
        "raw_json": json.dumps(
            {
                "video": {
                    "play_addr": {
                        "url_list": [
                            "https://cdn.test/direct.mp4",
                            "https://cdn.test/backup.mp4",
                        ]
                    }
                }
            }
        ),
    }
    assert candidate_video_urls(video) == [
        "https://cdn.test/direct.mp4",
        "https://cdn.test/backup.mp4",
    ]


def test_candidate_urls_convert_playwm_and_ignore_download_address() -> None:
    video = {
        "video_url": "https://cdn.test/playwm/?video_id=1",
        "raw_json": json.dumps(
            {
                "video": {
                    "play_addr": {"url_list": ["https://cdn.test/playwm/?video_id=1"]},
                    "download_addr": {"url_list": ["https://cdn.test/watermarked.mp4"]},
                }
            }
        ),
    }
    assert candidate_video_urls(video) == ["https://cdn.test/play/?video_id=1"]


def test_candidate_image_urls_ignore_watermarked_download_list() -> None:
    work = {
        "raw_json": json.dumps(
            {
                "images": [
                    {
                        "url_list": ["https://cdn.test/001.webp"],
                        "download_url_list": ["https://cdn.test/dy-water-001.webp"],
                    },
                    {"url_list": ["https://cdn.test/002.jpeg"]},
                ]
            }
        )
    }
    assert candidate_image_urls(work) == [
        "https://cdn.test/001.webp",
        "https://cdn.test/002.jpeg",
    ]

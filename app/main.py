from __future__ import annotations

import asyncio
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from app.browser import BrowserManager
from app.config import settings
from app.cover_cache import CoverCache
from app.db import Database
from app.douyin import DouyinScanner, InvalidProfileUrl, validate_profile_url
from app.downloader import VideoDownloader
from app.media import MediaPathError, inline_file_response, inline_image_response, list_local_images, resolve_download_path
from app.notifier import DingTalkConfigError, DingTalkNotifier
from app.schemas import (
    ContinueScanRequest,
    CreatorCreate,
    CreatorScheduleUpdate,
    CreatorUpdate,
    DeleteRequest,
    DingTalkUpdate,
    PreviewContinueRequest,
    PreviewConfirmationOptions,
    PreviewConfirmRequest,
    PreviewCreate,
    PreviewSelectionUpdate,
    PendingConfirmationRequest,
    SettingUpdate,
    VideoIdsRequest,
)
from app.service import SubscriptionService


settings.ensure_dirs()
db = Database(
    settings.resolved_database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_recycle_seconds=settings.database_pool_recycle_seconds,
    connect_retries=settings.database_connect_retries,
)
browser = BrowserManager(settings)
scanner = DouyinScanner(browser, settings)
downloader = VideoDownloader(browser, settings)
notifier = DingTalkNotifier(db, settings)
service = SubscriptionService(db, scanner, downloader, settings, notifier)
cover_cache = CoverCache(max_concurrency=2)
scheduler = AsyncIOScheduler(timezone="UTC")
STATIC_DIR = Path(__file__).resolve().parent / "static"
login_verification_notified = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    configured_download_dir = db.get_setting("download_dir")
    if configured_download_dir and not os.environ.get("DOUYIN_DOWNLOAD_DIR"):
        settings.download_dir = Path(configured_download_dir)
        settings.download_dir.mkdir(parents=True, exist_ok=True)
    await service.start()
    scheduler.add_job(
        service.scan_due_creators,
        "interval",
        seconds=settings.scan_poll_seconds,
        id="scan-due-creators",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    await service.resume_pending_scan_jobs()
    await service.scan_due_creators()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await service.shutdown()
        await browser.close()
        db.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.middleware("http")
async def prevent_stale_management_ui(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health", include_in_schema=False)
async def health() -> dict:
    db.fetch_one("SELECT 1 AS ok")
    cdp_ok = True
    if settings.browser_cdp_url:
        parsed = urlparse(settings.browser_cdp_url)
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(parsed.hostname or "127.0.0.1", parsed.port or 9222),
                timeout=2,
            )
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            cdp_ok = False
    if not cdp_ok:
        raise HTTPException(status_code=503, detail="Chrome CDP 不可用")
    return {
        "ok": True,
        "scheduler_running": scheduler.running,
        "browser_cdp_ok": cdp_ok,
    }


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def api_status() -> dict:
    global login_verification_notified
    login = await browser.login_status()
    if login.get("verification_required"):
        if not login_verification_notified and notifier.status()["enabled"]:
            login_verification_notified = True

            async def notify_login_verification() -> None:
                try:
                    await notifier.send(
                        "登录浏览器出现验证码",
                        {
                            "状态": "等待人工验证",
                            "处理方式": "回到电脑，在已打开的 Chrome 窗口完成验证",
                            "说明": "程序不会自动破解或绕过验证码",
                        },
                        "warning",
                    )
                except Exception as exc:
                    db.add_log("error", f"钉钉通知发送失败：{exc}")

            asyncio.create_task(notify_login_verification())
    else:
        login_verification_notified = False
    creators = db.list_creators()
    return {
        **login,
        "app_name": settings.app_name,
        "download_dir": str(settings.download_dir),
        "creator_count": len(creators),
        "active_scans": sum(service.task_running(int(item["id"])) for item in creators),
    }


@app.post("/api/login/open")
async def open_login() -> dict:
    try:
        await browser.open_login()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"无法启动登录浏览器：{exc}") from exc
    return {"ok": True, "message": "已打开抖音，请在浏览器中扫码或完成安全验证"}


@app.post("/api/login/close")
async def close_browser() -> dict:
    await browser.close()
    return {"ok": True}


@app.get("/api/creators")
async def list_creators(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    return db.list_creators_page(page=page, page_size=page_size)


@app.get("/api/creators/{creator_id}")
async def get_creator_detail(creator_id: int) -> dict:
    try:
        return db.get_creator_detail(creator_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc


@app.post("/api/creators", status_code=201)
async def create_creator(payload: CreatorCreate) -> dict:
    try:
        profile_url = validate_profile_url(str(payload.profile_url))
        creator = db.add_creator(
            profile_url,
            payload.interval_minutes,
            jitter_seconds=random.randint(0, settings.schedule_jitter_seconds),
            download_policy=payload.download_policy,
        )
    except InvalidProfileUrl as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="这个用户主页已经在监控列表中") from exc
    service.start_scan(int(creator["id"]), advance_schedule=True)
    return db.get_creator(int(creator["id"]))


@app.patch("/api/creators/{creator_id}")
async def update_creator(creator_id: int, payload: CreatorUpdate) -> dict:
    try:
        db.get_creator(creator_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    values = payload.model_dump(exclude_none=True)
    enabled = values.pop("enabled", None)
    interval_minutes = values.pop("interval_minutes", None)
    download_policy = values.pop("download_policy", None)
    if enabled is not None:
        db.update_creator(creator_id, enabled=bool(enabled))
        db.set_creator_schedule_enabled(creator_id, bool(enabled))
    if interval_minutes is not None:
        current = db.get_creator_schedule(creator_id)
        db.update_creator_schedule(
            creator_id,
            schedule_type="minutes",
            interval_value=int(interval_minutes),
            timezone_name=str(current["timezone"]),
            enabled=bool(current["enabled"]),
            jitter_seconds=int(current.get("jitter_seconds") or 0),
        )
    if download_policy is not None:
        db.update_creator(
            creator_id,
            download_policy=download_policy,
            policy_changed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    if values:
        db.update_creator(creator_id, **values)
    return db.get_creator_detail(creator_id)


@app.get("/api/creators/{creator_id}/schedule")
async def get_creator_schedule(creator_id: int) -> dict:
    try:
        return db.get_creator_schedule(creator_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户或调度配置不存在") from exc


@app.patch("/api/creators/{creator_id}/schedule")
async def update_creator_schedule(
    creator_id: int, payload: CreatorScheduleUpdate
) -> dict:
    try:
        current = db.get_creator_schedule(creator_id)
        return db.update_creator_schedule(
            creator_id,
            schedule_type=payload.schedule_type,
            interval_value=payload.interval_value,
            daily_time=payload.daily_time,
            timezone_name=payload.timezone,
            enabled=payload.enabled,
            jitter_seconds=int(current.get("jitter_seconds") or 0),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户或调度配置不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/creators/{creator_id}")
async def delete_creator(creator_id: int, payload: DeleteRequest) -> dict:
    try:
        return await service.delete_creator(
            creator_id, delete_local_files=payload.delete_local_files
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/creators/{creator_id}/scan", status_code=202)
async def scan_creator(creator_id: int) -> dict:
    try:
        db.get_creator(creator_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    if service.task_running(creator_id):
        return {"ok": True, "started": False, "message": "扫描已在运行"}
    db.update_creator(creator_id, status="idle", last_error=None)
    started = service.start_scan(creator_id, resume_paused=True)
    return {"ok": True, "started": started, "message": "扫描已启动" if started else "扫描已在运行"}


@app.post("/api/creators/{creator_id}/scan/continue", status_code=202)
async def continue_creator_scan(creator_id: int, payload: ContinueScanRequest) -> dict:
    try:
        db.get_creator(creator_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    started = service.start_scan(
        creator_id,
        job_type="continue",
        item_limit=payload.limit,
    )
    return {
        "ok": True,
        "started": started,
        "message": "已开始获取更早作品" if started else "已有扫描任务正在运行或暂停",
    }


@app.post("/api/previews", status_code=202)
async def create_preview(payload: PreviewCreate) -> dict:
    try:
        profile_url = validate_profile_url(str(payload.profile_url))
    except InvalidProfileUrl as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existing = db.find_creator_by_identity(profile_url=profile_url)
    if existing:
        raise HTTPException(status_code=409, detail="这个用户已经在监控列表中")
    preview = db.create_preview_session(
        profile_url,
        expires_in_minutes=settings.preview_session_ttl_minutes,
    )
    started = service.start_preview_scan(
        int(preview["id"]), item_limit=100, job_type="preview"
    )
    return {**db.get_preview_session(str(preview["token"])), "started": started}


@app.get("/api/previews/{token}")
async def get_preview(token: str) -> dict:
    try:
        return db.get_preview_session(token)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="预览会话不存在或已过期") from exc


@app.get("/api/previews/{token}/videos")
async def list_preview_videos(
    token: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
    keyword: str | None = None,
    content_type: str | None = Query(default=None, pattern="^(video|images)$"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> dict:
    try:
        return db.list_preview_videos(
            token,
            page=page,
            page_size=page_size,
            keyword=keyword,
            content_type=content_type,
            sort_order=sort_order,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="预览会话不存在或已过期") from exc


@app.get("/api/previews/{token}/videos/{preview_video_id}/cover")
async def stream_preview_cover(token: str, preview_video_id: int):
    try:
        video = db.get_preview_video(token, preview_video_id)
        try:
            path = await cover_cache.ensure_local(video, settings.download_dir)
        except Exception:
            if video.get("cover_url"):
                return RedirectResponse(str(video["cover_url"]), status_code=307)
            raise FileNotFoundError
        return inline_image_response(path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="预览作品不存在或已过期") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="预览封面不存在") from exc


@app.post("/api/previews/{token}/continue", status_code=202)
async def continue_preview(token: str, payload: PreviewContinueRequest) -> dict:
    try:
        preview = db.get_preview_session(token)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="预览会话不存在或已过期") from exc
    if preview["status"] in {"confirmed", "duplicate", "cancelled"}:
        raise HTTPException(status_code=409, detail="当前预览会话不能继续扫描")
    db.update_preview_session(int(preview["id"]), status="queued", last_error=None)
    started = service.start_preview_scan(
        int(preview["id"]),
        job_type="preview_continue",
        item_limit=payload.limit,
    )
    if not started:
        raise HTTPException(status_code=409, detail="已有预览扫描正在运行或暂停")
    return {**db.get_preview_session(token), "started": True}


@app.post("/api/previews/{token}/cancel", status_code=202)
async def cancel_preview(token: str) -> dict:
    try:
        preview = db.get_preview_session(token)
        return service.cancel_preview_scan(int(preview["id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="预览会话不存在或已过期") from exc


@app.patch("/api/previews/{token}/selection")
async def update_preview_selection(token: str, payload: PreviewSelectionUpdate) -> dict:
    try:
        return db.update_preview_selection(
            token,
            action=payload.action,
            aweme_ids=payload.aweme_ids,
            selection_filter=payload.filter,
            auto_select_new=payload.auto_select_new,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="预览会话不存在或已过期") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/previews/{token}/confirmation-summary")
async def preview_confirmation_summary(
    token: str, payload: PreviewConfirmationOptions
) -> dict:
    try:
        return db.preview_confirmation_summary(
            token,
            download_policy=payload.download_policy,
            immediate_download_selected=payload.immediate_download_selected,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="预览会话不存在或已过期") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/previews/{token}/confirm", status_code=201)
async def confirm_preview(token: str, payload: PreviewConfirmRequest) -> dict:
    try:
        result = db.confirm_preview_session(
            token,
            idempotency_key=payload.idempotency_key,
            download_policy=payload.download_policy,
            immediate_download_selected=payload.immediate_download_selected,
            schedule_type=payload.schedule_type,
            interval_value=payload.interval_value,
            daily_time=payload.daily_time,
            timezone_name=payload.timezone,
            jitter_seconds=random.randint(0, settings.schedule_jitter_seconds),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="预览会话不存在或已过期") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result["download_jobs_created"]:
        service.wake_download_workers()
    creator_id = int(result["creator"]["id"])
    return {**result, "redirect_url": f"/creators/{creator_id}"}


@app.get("/api/scan-jobs")
async def list_scan_jobs(
    creator_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    statuses = {status} if status else None
    return db.list_scan_jobs(creator_id=creator_id, statuses=statuses, limit=limit)


@app.get("/api/scan-jobs/{job_id}")
async def get_scan_job(job_id: int) -> dict:
    try:
        return db.get_scan_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc


@app.post("/api/scan-jobs/{job_id}/pause", status_code=202)
async def pause_scan_job(job_id: int) -> dict:
    try:
        return service.pause_scan_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc


@app.post("/api/scan-jobs/{job_id}/cancel", status_code=202)
async def cancel_scan_job(job_id: int) -> dict:
    try:
        return service.cancel_scan_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc


@app.post("/api/scan-jobs/{job_id}/resume", status_code=202)
async def resume_scan_job(job_id: int) -> dict:
    try:
        return service.resume_scan_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/videos")
async def list_videos(
    creator_id: int | None = None,
    status: str | None = None,
    content_type: str | None = None,
    keyword: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="newest", pattern="^(newest|oldest)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=256),
) -> dict:
    try:
        return db.list_videos_page(
            creator_id=creator_id,
            status=status,
            content_type=content_type,
            keyword=keyword,
            sort=sort,
            page=page,
            page_size=page_size,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/media/videos/{video_id}")
async def stream_local_video(video_id: int):
    try:
        video = db.get_video(video_id)
        if video.get("content_type") == "images" or not video.get("file_path"):
            raise FileNotFoundError
        path = resolve_download_path(settings.download_dir, str(video["file_path"]))
        return inline_file_response(path, default_media_type="video/mp4")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品记录不存在") from exc
    except (FileNotFoundError, MediaPathError) as exc:
        raise HTTPException(status_code=404, detail="本地视频文件不存在") from exc


@app.get("/api/videos/{video_id}/playback-context")
async def get_video_playback_context(
    video_id: int,
    creator_id: int | None = None,
    status: str | None = None,
    content_type: str | None = None,
    keyword: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="newest", pattern="^(newest|oldest)$"),
) -> dict:
    try:
        return db.get_video_playback_context(
            video_id,
            creator_id=creator_id,
            status=status,
            content_type=content_type,
            keyword=keyword,
            sort=sort,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品不在当前播放列表中") from exc


@app.get("/api/media/videos/{video_id}/cover")
async def stream_local_cover(video_id: int):
    try:
        video = db.get_video(video_id)
        try:
            if not video.get("cover_path"):
                raise FileNotFoundError
            path = resolve_download_path(settings.download_dir, str(video["cover_path"]))
        except (FileNotFoundError, MediaPathError):
            try:
                path = await cover_cache.ensure_local(video, settings.download_dir)
                db.update_video(video_id, cover_path=str(path))
            except Exception:
                if video.get("cover_url"):
                    return RedirectResponse(str(video["cover_url"]), status_code=307)
                raise FileNotFoundError
        return inline_image_response(path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品记录不存在") from exc
    except (FileNotFoundError, MediaPathError) as exc:
        raise HTTPException(status_code=404, detail="本地封面不存在") from exc


@app.get("/api/media/videos/{video_id}/assets")
async def list_local_image_assets(video_id: int) -> dict:
    try:
        video = db.get_video(video_id)
        if video.get("content_type") != "images" or not video.get("file_path"):
            raise FileNotFoundError
        paths = list_local_images(settings.download_dir, str(video["file_path"]))
        return {
            "video_id": video_id,
            "items": [
                {
                    "position": index,
                    "url": f"/api/media/videos/{video_id}/assets/{index}",
                    "name": path.name,
                }
                for index, path in enumerate(paths, start=1)
            ],
            "total": len(paths),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品记录不存在") from exc
    except (FileNotFoundError, MediaPathError) as exc:
        raise HTTPException(status_code=404, detail="本地图文资源不存在") from exc


@app.get("/api/media/videos/{video_id}/assets/{position}")
async def stream_local_image_asset(video_id: int, position: int):
    try:
        video = db.get_video(video_id)
        if video.get("content_type") != "images" or not video.get("file_path"):
            raise FileNotFoundError
        paths = list_local_images(settings.download_dir, str(video["file_path"]))
        if position < 1 or position > len(paths):
            raise FileNotFoundError
        return inline_image_response(paths[position - 1])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品记录不存在") from exc
    except (FileNotFoundError, MediaPathError) as exc:
        raise HTTPException(status_code=404, detail="本地图文资源不存在") from exc


@app.get("/api/pending-confirmations")
async def list_pending_confirmations(
    creator_id: int | None = None,
    content_type: str | None = None,
    keyword: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="newest", pattern="^(newest|oldest)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
) -> dict:
    return db.list_videos_page(
        creator_id=creator_id,
        content_type=content_type,
        keyword=keyword,
        needs_confirmation=True,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@app.post("/api/pending-confirmations/resolve", status_code=202)
async def resolve_pending_confirmations(payload: PendingConfirmationRequest) -> dict:
    try:
        return service.resolve_pending_confirmations(
            payload.video_ids, download=payload.action == "download"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="部分作品记录不存在") from exc


@app.post("/api/videos/{video_id}/retry", status_code=202)
async def retry_video(video_id: int) -> dict:
    try:
        jobs = service.queue_manual_video_downloads([video_id])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="视频记录不存在") from exc
    return {"ok": True, "queued": True, "download_job": jobs[0]}


@app.post("/api/videos/{video_id}/download", status_code=202)
async def download_video(video_id: int) -> dict:
    try:
        jobs = service.queue_manual_video_downloads([video_id])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="视频记录不存在") from exc
    return {"ok": True, "queued": True, "download_job": jobs[0]}


@app.post("/api/videos/bulk-download", status_code=202)
async def bulk_download_videos(payload: VideoIdsRequest) -> dict:
    try:
        jobs = service.queue_manual_video_downloads(payload.video_ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="部分视频记录不存在") from exc
    return {"ok": True, "queued_count": len(jobs), "download_jobs": jobs}


@app.post("/api/videos/bulk-retry", status_code=202)
async def bulk_retry_videos(payload: VideoIdsRequest) -> dict:
    try:
        result = service.retry_failed_videos(payload.video_ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="部分视频记录不存在") from exc
    return {"ok": True, **result}


@app.delete("/api/videos/{video_id}")
async def delete_video(video_id: int, payload: DeleteRequest) -> dict:
    try:
        return await service.delete_video(
            video_id, delete_local_files=payload.delete_local_files
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="视频记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/download-jobs")
async def list_download_jobs(
    creator_id: int | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
) -> dict:
    return db.list_download_jobs_page(
        creator_id=creator_id,
        statuses={status} if status else None,
        page=page,
        page_size=page_size,
    )


@app.get("/api/download-jobs/{job_id}")
async def get_download_job(job_id: int) -> dict:
    try:
        return db.get_download_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="下载任务不存在") from exc


@app.post("/api/download-jobs/{job_id}/pause", status_code=202)
async def pause_download_job(job_id: int) -> dict:
    try:
        return service.pause_download_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="下载任务不存在") from exc


@app.post("/api/download-jobs/{job_id}/resume", status_code=202)
async def resume_download_job(job_id: int) -> dict:
    try:
        return service.resume_download_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="下载任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/download-jobs/{job_id}/cancel", status_code=202)
async def cancel_download_job(job_id: int) -> dict:
    try:
        return service.cancel_download_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="下载任务不存在") from exc


@app.post("/api/download-jobs/{job_id}/retry", status_code=202)
async def retry_download_job(job_id: int) -> dict:
    try:
        return service.retry_download_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="下载任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/logs")
async def list_logs(
    level: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict:
    return db.list_logs_page(level=level, page=page, page_size=page_size)


@app.get("/api/settings")
async def get_settings() -> dict:
    return {
        "download_dir": str(settings.download_dir),
        "download_dir_locked": bool(os.environ.get("DOUYIN_DOWNLOAD_DIR")),
        "default_interval_minutes": settings.default_interval_minutes,
    }


@app.patch("/api/settings")
async def update_settings(payload: SettingUpdate) -> dict:
    if payload.download_dir is not None:
        if os.environ.get("DOUYIN_DOWNLOAD_DIR"):
            raise HTTPException(
                status_code=409,
                detail="下载目录由 DOUYIN_DOWNLOAD_DIR 环境变量锁定，请修改容器挂载配置",
            )
        candidate = Path(payload.download_dir).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"无法使用这个下载目录：{exc}") from exc
        if not candidate.is_dir():
            raise HTTPException(status_code=400, detail="下载路径不是文件夹")
        settings.download_dir = candidate
        db.set_setting("download_dir", str(candidate))
    return await get_settings()


@app.get("/api/notifications/dingtalk")
async def get_dingtalk() -> dict:
    return notifier.status()


@app.patch("/api/notifications/dingtalk")
async def update_dingtalk(payload: DingTalkUpdate) -> dict:
    try:
        return notifier.configure(payload.enabled, payload.webhook, payload.secret)
    except DingTalkConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/notifications/dingtalk/test")
async def test_dingtalk() -> dict:
    try:
        sent = await notifier.send(
            "抖音下载器测试通知",
            {"状态": "Webhook 与加签配置有效", "服务地址": "http://127.0.0.1:8765"},
            "success",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"测试通知失败：{exc}") from exc
    if not sent:
        raise HTTPException(status_code=400, detail="钉钉通知尚未启用")
    return {"ok": True}

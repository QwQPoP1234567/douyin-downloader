from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
import shutil
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import Database, utc_now
from app.douyin import DouyinScanner, VerificationRequired
from app.downloader import (
    DownloadCancelled,
    DownloadControlRequested,
    DownloadPaused,
    VideoDownloader,
    candidate_image_urls,
    safe_name,
)
from app.notifier import DingTalkNotifier
from app.policies import should_auto_download, should_request_confirmation


class SubscriptionService:
    def __init__(
        self,
        db: Database,
        scanner: DouyinScanner,
        downloader: VideoDownloader,
        settings: Settings,
        notifier: DingTalkNotifier,
    ):
        self.db = db
        self.scanner = scanner
        self.downloader = downloader
        self.settings = settings
        self.notifier = notifier
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._preview_tasks: dict[int, asyncio.Task[None]] = {}
        self._download_tasks: list[asyncio.Task[None]] = []
        self._download_stopping = asyncio.Event()
        self._download_wakeup = asyncio.Event()

    async def start(self) -> None:
        if any(not task.done() for task in self._download_tasks):
            return
        cleanup_temp_files = getattr(self.downloader, "cleanup_stale_temp_files", None)
        removed_temp_files = cleanup_temp_files() if cleanup_temp_files else 0
        if removed_temp_files:
            self.db.add_log("info", f"启动时清理了 {removed_temp_files} 个无效下载临时文件")
        self._download_stopping.clear()
        concurrency = max(1, min(int(self.settings.download_concurrency), 3))
        worker_prefix = f"{socket.gethostname()}:{os.getpid()}"
        self._download_tasks = [
            asyncio.create_task(
                self._download_worker(f"{worker_prefix}:{index}"),
                name=f"download-worker-{index}",
            )
            for index in range(1, concurrency + 1)
        ]

    def wake_download_workers(self) -> None:
        self._download_wakeup.set()

    def pause_download_job(self, job_id: int) -> dict[str, Any]:
        return self.db.request_download_job_pause(job_id)

    def cancel_download_job(self, job_id: int) -> dict[str, Any]:
        return self.db.request_download_job_cancel(job_id)

    def resume_download_job(self, job_id: int) -> dict[str, Any]:
        job = self.db.resume_download_job(job_id)
        self.wake_download_workers()
        return job

    def retry_download_job(self, job_id: int) -> dict[str, Any]:
        job = self.db.retry_download_job(job_id, priority=100)
        self.wake_download_workers()
        return job

    def queue_manual_video_downloads(self, video_ids: list[int]) -> list[dict[str, Any]]:
        videos = self.db.get_videos(video_ids)
        by_creator: dict[int, list[int]] = {}
        for video in videos:
            video_id = int(video["id"])
            creator_id = int(video["creator_id"])
            if video.get("status") != "downloading":
                self.db.update_video(video_id, status="pending", last_error=None)
            by_creator.setdefault(creator_id, []).append(video_id)

        jobs: list[dict[str, Any]] = []
        for creator_id, creator_video_ids in by_creator.items():
            jobs.extend(
                self.db.enqueue_download_jobs(
                    creator_id,
                    creator_video_ids,
                    priority=100,
                    force=True,
                )
            )
        if jobs:
            self.wake_download_workers()
        return jobs

    def retry_failed_videos(self, video_ids: list[int]) -> dict[str, Any]:
        videos = self.db.get_videos(video_ids)
        failed_ids = [
            int(video["id"])
            for video in videos
            if video.get("status") == "failed"
        ]
        jobs = self.queue_manual_video_downloads(failed_ids) if failed_ids else []
        return {
            "download_jobs": jobs,
            "queued_count": len(jobs),
            "skipped_count": len(videos) - len(failed_ids),
        }

    def resolve_pending_confirmations(
        self, video_ids: list[int], *, download: bool
    ) -> dict[str, Any]:
        videos = self.db.get_videos(video_ids)
        pending = [video for video in videos if video.get("needs_confirmation")]
        pending_ids = [int(video["id"]) for video in pending]
        if pending_ids:
            self.db.bulk_update_videos(
                [{"id": video_id, "needs_confirmation": False} for video_id in pending_ids]
            )
        jobs = self.queue_manual_video_downloads(pending_ids) if download and pending_ids else []
        creator_ids = {int(video["creator_id"]) for video in pending}
        for creator_id in creator_ids:
            self.db.recount_creator(creator_id)
        return {
            "ok": True,
            "resolved_count": len(pending_ids),
            "queued_count": len(jobs),
            "skipped_count": len(videos) - len(pending_ids),
        }

    async def delete_video(
        self, video_id: int, *, delete_local_files: bool = False
    ) -> dict[str, Any]:
        video = self.db.get_video(video_id)
        creator_id = int(video["creator_id"])
        paths: list[Path] = []
        if delete_local_files:
            download_root = self.settings.download_dir.resolve()
            for value in (video.get("file_path"), video.get("cover_path")):
                if not value:
                    continue
                path = Path(str(value)).resolve()
                if path == download_root or download_root not in path.parents:
                    raise ValueError(f"拒绝删除下载目录之外的路径：{path}")
                if path not in paths:
                    paths.append(path)
            # Delete nested files before their containing image-work directory.
            for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        shutil.rmtree(path)
                except OSError as exc:
                    raise RuntimeError(f"删除本地文件失败：{path}：{exc}") from exc

        self.db.delete_video(video_id)
        self.db.recount_creator(creator_id)
        self.db.add_log(
            "warning",
            f"已删除作品记录 {video['aweme_id']}"
            + ("及本地文件" if delete_local_files else "，本地文件已保留"),
            creator_id,
            video_id=None,
        )
        await self._rewrite_metadata(creator_id)
        return {
            "ok": True,
            "video_id": video_id,
            "local_files_deleted": delete_local_files,
        }

    async def delete_creator(
        self, creator_id: int, *, delete_local_files: bool = False
    ) -> dict[str, Any]:
        summary = self.db.prepare_creator_deletion(creator_id)
        task = self._tasks.get(creator_id)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.db.finalize_creator_scan_cancellation(creator_id)
        summary = self.db.prepare_creator_deletion(creator_id)
        if int(summary["active_download_jobs"]) > 0:
            raise RuntimeError("下载任务正在停止，请稍后重试删除")

        paths: list[Path] = []
        if delete_local_files:
            download_root = self.settings.download_dir.resolve()
            for value in summary["local_paths"]:
                path = Path(str(value)).resolve()
                if path == download_root or download_root not in path.parents:
                    raise ValueError(f"拒绝删除下载目录之外的路径：{path}")
                if path not in paths:
                    paths.append(path)
            for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        shutil.rmtree(path)
                except OSError as exc:
                    raise RuntimeError(f"删除本地文件失败：{path}：{exc}") from exc

        creator = summary["creator"]
        self.db.add_log(
            "warning",
            f"已删除监控用户 {creator.get('nickname') or creator_id}，"
            f"共 {summary['video_count']} 个作品、{summary['downloaded_count']} 个已下载文件；"
            + ("本地文件已删除" if delete_local_files else "本地文件已保留"),
            creator_id,
        )
        self.db.delete_creator(creator_id)
        return {
            "ok": True,
            "creator_id": creator_id,
            "video_count": summary["video_count"],
            "downloaded_count": summary["downloaded_count"],
            "local_files_deleted": delete_local_files,
        }

    async def _download_worker(self, worker_id: str) -> None:
        while not self._download_stopping.is_set():
            try:
                jobs = self.db.claim_download_jobs(worker_id, limit=1)
            except Exception as exc:
                self.db.add_log("error", f"下载队列领取失败：{exc}")
                continue
            if not jobs:
                self._download_wakeup.clear()
                try:
                    await asyncio.wait_for(self._download_wakeup.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            await self._process_download_job(jobs[0])

    @staticmethod
    def _download_retry_delay(attempts: int) -> int:
        # The downloader already performs immediate network retries inside one attempt. The
        # persistent queue then uses a graded retry schedule, with the first requeue immediate.
        return (0, 30, 120, 600, 1800)[min(max(attempts - 1, 0), 4)]

    async def _process_download_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        creator_id = int(job["creator_id"])
        try:
            video = self.db.get_video(int(job["video_id"]))
            downloaded = await self._download_one(creator_id, video, job_id=job_id)
            if not downloaded:
                return
            completed_video = self.db.get_video(int(job["video_id"]))
            self.db.update_download_job(
                job_id,
                status="completed",
                bytes_downloaded=int(completed_video.get("file_size") or 0),
                total_bytes=int(completed_video.get("file_size") or 0),
                speed_bytes_per_second=0,
                locked_by=None,
                locked_at=None,
                heartbeat_at=utc_now(),
                failure_reason=None,
                finished_at=utc_now(),
            )
            await self._rewrite_metadata(creator_id)
        except DownloadPaused:
            self.db.update_download_job(
                job_id,
                status="paused",
                locked_by=None,
                locked_at=None,
                heartbeat_at=utc_now(),
                failure_reason="用户暂停下载",
            )
        except DownloadCancelled:
            self.db.update_download_job(
                job_id,
                status="cancelled",
                locked_by=None,
                locked_at=None,
                heartbeat_at=utc_now(),
                failure_reason="用户取消下载",
                finished_at=utc_now(),
            )
        except VerificationRequired as exc:
            self.db.update_download_job(
                job_id,
                status="paused",
                pause_requested=True,
                locked_by=None,
                locked_at=None,
                heartbeat_at=utc_now(),
                failure_reason=str(exc),
            )
        except KeyError:
            # Deleting a video cascades its download job; no final update is necessary.
            return
        except Exception as exc:
            attempts = int(job.get("attempts") or 1)
            max_attempts = int(job.get("max_attempts") or 5)
            if attempts < max_attempts:
                delay = self._download_retry_delay(attempts)
                next_attempt = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat(timespec="seconds")
                self.db.update_download_job(
                    job_id,
                    status="queued",
                    next_attempt_at=next_attempt,
                    locked_by=None,
                    locked_at=None,
                    heartbeat_at=utc_now(),
                    failure_reason=str(exc),
                )
                self.db.update_video(
                    int(job["video_id"]), status="pending", last_error=str(exc)
                )
                if delay == 0:
                    self.wake_download_workers()
            else:
                self.db.update_download_job(
                    job_id,
                    status="failed",
                    locked_by=None,
                    locked_at=None,
                    heartbeat_at=utc_now(),
                    failure_reason=str(exc),
                    finished_at=utc_now(),
                )

    def task_running(self, creator_id: int) -> bool:
        task = self._tasks.get(creator_id)
        return bool(task and not task.done())

    def _prepare_scan_job(
        self,
        creator_id: int,
        *,
        job_id: int | None = None,
        job_type: str | None = None,
        resume_paused: bool = False,
        item_limit: int | None = None,
    ) -> dict[str, Any] | None:
        creator = self.db.get_creator(creator_id)
        if job_id is not None:
            job = self.db.get_scan_job(job_id)
            if int(job.get("creator_id") or 0) != creator_id:
                raise ValueError("Scan job does not belong to this creator")
        else:
            inferred_type = job_type or (
                "initial" if int(creator.get("total_found") or 0) == 0 else "incremental"
            )
            job, _ = self.db.create_scan_job(
                creator_id=creator_id,
                job_type=inferred_type,
                item_limit=int(item_limit or creator.get("per_scan_limit") or 100),
                max_scrolls=int(self.settings.max_scan_scrolls),
                max_runtime_seconds=int(self.settings.scan_max_runtime_seconds),
            )
        if job["status"] == "paused" and resume_paused:
            job = self.db.resume_scan_job(int(job["id"]))
        return job if job["status"] == "queued" else None

    def start_scan(
        self,
        creator_id: int,
        *,
        job_id: int | None = None,
        job_type: str | None = None,
        resume_paused: bool = False,
        item_limit: int | None = None,
        advance_schedule: bool = False,
    ) -> bool:
        if self.task_running(creator_id):
            return False
        job = self._prepare_scan_job(
            creator_id,
            job_id=job_id,
            job_type=job_type,
            resume_paused=resume_paused,
            item_limit=item_limit,
        )
        if job is None:
            return False
        persisted_job_id = int(job["id"])
        task = asyncio.create_task(self.scan_creator(creator_id, persisted_job_id))
        self._tasks[creator_id] = task

        def clean_up(done: asyncio.Task[None]) -> None:
            self._tasks.pop(creator_id, None)
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.db.add_log("error", f"后台扫描任务异常：{exc}", creator_id)
                asyncio.create_task(
                    self._notify(
                        "后台扫描任务异常",
                        {"用户ID": creator_id, "错误详情": str(exc)},
                        "error",
                    )
                )

        task.add_done_callback(clean_up)
        if advance_schedule:
            self.db.record_creator_schedule_run(creator_id)
        return True

    async def resume_pending_scan_jobs(self) -> None:
        jobs = self.db.list_scan_jobs(statuses={"queued"}, limit=1000)
        for job in jobs:
            creator_id = job.get("creator_id")
            if creator_id is not None:
                self.start_scan(int(creator_id), job_id=int(job["id"]))
            elif job.get("preview_session_id") is not None:
                self.start_preview_scan(
                    int(job["preview_session_id"]), job_id=int(job["id"])
                )

    def preview_task_running(self, preview_session_id: int) -> bool:
        task = self._preview_tasks.get(preview_session_id)
        return bool(task and not task.done())

    def start_preview_scan(
        self,
        preview_session_id: int,
        *,
        job_id: int | None = None,
        job_type: str = "preview",
        item_limit: int = 100,
    ) -> bool:
        if self.preview_task_running(preview_session_id):
            return False
        preview = self.db.get_preview_session_by_id(preview_session_id)
        if preview["status"] in {"confirmed", "expired", "cancelled", "duplicate"}:
            return False
        if job_id is None:
            job, _ = self.db.create_scan_job(
                preview_session_id=preview_session_id,
                job_type=job_type,
                item_limit=item_limit,
                max_scrolls=int(self.settings.max_scan_scrolls),
                max_runtime_seconds=int(self.settings.scan_max_runtime_seconds),
            )
        else:
            job = self.db.get_scan_job(job_id)
        if job["status"] != "queued":
            return False
        task = asyncio.create_task(
            self.scan_preview_session(preview_session_id, int(job["id"])),
            name=f"preview-scan-{preview_session_id}",
        )
        self._preview_tasks[preview_session_id] = task

        def clean_up(done: asyncio.Task[None]) -> None:
            self._preview_tasks.pop(preview_session_id, None)
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.db.add_log("error", f"预览扫描后台异常：{exc}")

        task.add_done_callback(clean_up)
        return True

    async def scan_preview_session(self, preview_session_id: int, job_id: int) -> None:
        preview = self.db.get_preview_session_by_id(preview_session_id)
        claimed = self.db.claim_scan_job(job_id)
        if claimed is None:
            return
        written_count = int(claimed.get("written_count") or 0)
        skip_aweme_ids = (
            set(self.db.list_preview_aweme_ids(preview_session_id))
            if claimed["job_type"] == "preview_continue" or written_count > 0
            else None
        )
        self.db.update_preview_session(
            preview_session_id, status="scanning", last_error=None
        )

        async def persist_batch(items: list[dict[str, Any]]) -> None:
            nonlocal written_count
            ids = self.db.bulk_upsert_preview_videos(preview_session_id, items)
            written_count += len(ids)
            self.db.update_scan_job(
                job_id,
                written_count=written_count,
                heartbeat_at=utc_now(),
            )

        async def persist_progress(progress: dict[str, Any]) -> None:
            self.db.update_scan_job(
                job_id,
                scroll_count=int(progress.get("scroll_count") or 0),
                discovered_count=int(progress.get("discovered_count") or 0),
                cursor=progress.get("cursor"),
                progress_json=progress,
                heartbeat_at=utc_now(),
            )

        async def check_control() -> str | None:
            current = self.db.get_scan_job(job_id)
            if current["cancel_requested"]:
                return "cancel"
            if current["pause_requested"]:
                return "pause"
            return None

        try:
            result = await self.scanner.scan_profile(
                str(preview["submitted_url"]),
                item_limit=int(claimed["item_limit"]),
                batch_size=int(self.settings.scan_batch_size),
                max_scrolls=int(claimed["max_scrolls"]),
                max_runtime_seconds=int(claimed["max_runtime_seconds"]),
                no_progress_seconds=int(self.settings.scan_no_progress_seconds),
                on_batch=persist_batch,
                on_progress=persist_progress,
                check_control=check_control,
                skip_aweme_ids=skip_aweme_ids,
            )
            if result.videos:
                await persist_batch(result.videos)
            current = self.db.get_scan_job(job_id)
            if current["cancel_requested"] or result.stop_reason == "cancel":
                self.db.update_scan_job(
                    job_id, status="cancelled", ended_at=utc_now(), heartbeat_at=utc_now()
                )
                self.db.update_preview_session(preview_session_id, status="cancelled")
                return
            if current["pause_requested"] or result.stop_reason == "pause":
                self.db.update_scan_job(
                    job_id, status="paused", ended_at=utc_now(), heartbeat_at=utc_now()
                )
                self.db.update_preview_session(preview_session_id, status="paused")
                return
            duplicate = self.db.find_creator_by_identity(
                profile_url=result.profile_url, sec_uid=result.sec_uid
            )
            all_preview_ids = self.db.list_preview_aweme_ids(preview_session_id)
            final_status = "duplicate" if duplicate else "completed"
            last_error = (
                f"该用户已在监控列表中（用户 ID：{duplicate['id']}）"
                if duplicate
                else None
            )
            self.db.update_preview_session(
                preview_session_id,
                normalized_url=result.profile_url,
                sec_uid=result.sec_uid,
                nickname=result.nickname,
                avatar_url=result.avatar_url,
                status=final_status,
                discovered_count=len(all_preview_ids),
                last_error=last_error,
            )
            self.db.update_scan_job(
                job_id,
                status="completed",
                discovered_count=len(result.aweme_ids or []),
                written_count=written_count,
                scroll_count=result.scroll_count,
                cursor=result.cursor,
                ended_at=utc_now(),
                heartbeat_at=utc_now(),
                progress_json={
                    "complete": result.complete,
                    "expected_count": result.expected_count,
                    "stop_reason": result.stop_reason,
                },
                failure_reason=last_error,
            )
        except VerificationRequired as exc:
            self.db.update_scan_job(
                job_id,
                status="paused",
                pause_requested=True,
                ended_at=utc_now(),
                heartbeat_at=utc_now(),
                failure_reason=str(exc),
            )
            self.db.update_preview_session(
                preview_session_id, status="paused", last_error=str(exc)
            )
        except asyncio.CancelledError:
            current = self.db.get_scan_job(job_id)
            if current["status"] not in {"completed", "failed", "cancelled"}:
                self.db.update_scan_job(
                    job_id,
                    status="queued",
                    heartbeat_at=utc_now(),
                    failure_reason="服务关闭，预览任务等待恢复",
                )
                self.db.update_preview_session(preview_session_id, status="queued")
            raise
        except Exception as exc:
            self.db.update_scan_job(
                job_id,
                status="failed",
                ended_at=utc_now(),
                heartbeat_at=utc_now(),
                failure_reason=str(exc),
            )
            self.db.update_preview_session(
                preview_session_id, status="failed", last_error=str(exc)
            )

    def cancel_preview_scan(self, preview_session_id: int) -> dict[str, Any]:
        active = self.db.get_active_scan_job(preview_session_id=preview_session_id)
        if active is not None:
            self.db.request_scan_job_cancel(int(active["id"]))
        return self.db.update_preview_session(preview_session_id, status="cancelled")

    def pause_scan_job(self, job_id: int) -> dict[str, Any]:
        return self.db.request_scan_job_pause(job_id)

    def cancel_scan_job(self, job_id: int) -> dict[str, Any]:
        return self.db.request_scan_job_cancel(job_id)

    def resume_scan_job(self, job_id: int) -> dict[str, Any]:
        job = self.db.resume_scan_job(job_id)
        creator_id = job.get("creator_id")
        started = False
        if creator_id is not None:
            started = self.start_scan(int(creator_id), job_id=job_id)
        return {**job, "started": started}

    async def scan_due_creators(self) -> None:
        self.db.cleanup_expired_preview_sessions()
        due = self.db.list_due_creator_schedules(limit=1000)
        for schedule in due:
            if schedule.get("creator_status") in {"scanning", "needs_verification"}:
                continue
            self.start_scan(int(schedule["creator_id"]), advance_schedule=True)

    async def scan_creator(self, creator_id: int, job_id: int | None = None) -> None:
        job = self._prepare_scan_job(creator_id, job_id=job_id)
        if job is None:
            return
        job_id = int(job["id"])
        claimed = self.db.claim_scan_job(job_id)
        if claimed is None:
            return
        creator = self.db.get_creator(creator_id)
        scan_policy = str(creator.get("download_policy") or "metadata_only")
        next_scan = self.db.get_creator_schedule(creator_id).get("next_run_at")
        self.db.update_creator(
            creator_id,
            status="scanning",
            last_error=None,
        )
        self.db.add_log("info", "开始扫描用户作品", creator_id)
        scan_completed = False
        new_count = 0
        written_count = 0
        new_video_ids: list[int] = []
        skip_aweme_ids: set[str] | None = None
        if claimed["job_type"] == "continue" or int(claimed["written_count"] or 0) > 0:
            skip_aweme_ids = set(self.db.list_video_aweme_ids(creator_id))

        async def persist_batch(items: list[dict[str, Any]]) -> None:
            nonlocal new_count, written_count
            results = self.db.bulk_upsert_videos(creator_id, items)
            new_count += sum(1 for _, created in results if created)
            created_ids = [video_id for video_id, created in results if created]
            new_video_ids.extend(created_ids)
            if created_ids:
                needs_confirmation = should_request_confirmation(
                    scan_policy, str(claimed["job_type"])
                )
                self.db.bulk_update_videos(
                    [
                        {
                            "id": video_id,
                            "policy_snapshot": scan_policy,
                            "needs_confirmation": needs_confirmation,
                        }
                        for video_id in created_ids
                    ]
                )
            written_count += len(results)
            self.db.update_scan_job(
                job_id,
                written_count=written_count,
                heartbeat_at=utc_now(),
            )

        async def persist_progress(progress: dict[str, Any]) -> None:
            self.db.update_scan_job(
                job_id,
                scroll_count=int(progress.get("scroll_count") or 0),
                discovered_count=int(progress.get("discovered_count") or 0),
                cursor=progress.get("cursor"),
                progress_json=progress,
                heartbeat_at=utc_now(),
            )

        async def scan_control() -> str | None:
            current = self.db.get_scan_job(job_id)
            if current["cancel_requested"]:
                return "cancel"
            if current["pause_requested"]:
                return "pause"
            return None

        try:
            result = await self.scanner.scan_profile(
                creator["profile_url"],
                item_limit=int(claimed["item_limit"]),
                batch_size=int(self.settings.scan_batch_size),
                max_scrolls=int(claimed["max_scrolls"]),
                max_runtime_seconds=int(claimed["max_runtime_seconds"]),
                no_progress_seconds=int(self.settings.scan_no_progress_seconds),
                on_batch=persist_batch,
                on_progress=persist_progress,
                check_control=scan_control,
                skip_aweme_ids=skip_aweme_ids,
            )
            # Compatibility for alternate scanners that return a materialized result and do not
            # implement the streaming callback yet.
            if result.videos:
                await persist_batch(result.videos)
            discovered_aweme_ids = result.aweme_ids or [
                str(item["aweme_id"]) for item in result.videos
            ]
            seen_aweme_ids = result.encountered_aweme_ids or discovered_aweme_ids
            discovered_count = len(discovered_aweme_ids)
            self.db.update_scan_job(
                job_id,
                scroll_count=result.scroll_count,
                discovered_count=discovered_count,
                written_count=written_count,
                cursor=result.cursor,
                heartbeat_at=utc_now(),
                progress_json={
                    "complete": result.complete,
                    "expected_count": result.expected_count,
                    "discarded_count": result.discarded_count,
                    "stop_reason": result.stop_reason,
                },
            )
            self.db.update_creator(
                creator_id,
                profile_url=result.profile_url,
                nickname=result.nickname or creator.get("nickname"),
                sec_uid=result.sec_uid or creator.get("sec_uid"),
            )
            if result.sec_uid:
                await self._cleanup_foreign_videos(creator_id, result.sec_uid)
            control = self.db.get_scan_job(job_id)
            if control["cancel_requested"]:
                self.db.update_scan_job(
                    job_id,
                    status="cancelled",
                    ended_at=utc_now(),
                    heartbeat_at=utc_now(),
                )
                self.db.update_creator(creator_id, status="idle", last_error=None)
                return
            if control["pause_requested"]:
                self.db.update_scan_job(
                    job_id,
                    status="paused",
                    ended_at=utc_now(),
                    heartbeat_at=utc_now(),
                )
                self.db.update_creator(creator_id, status="idle", last_error=None)
                return
            removed_items: list[dict[str, Any]] = []
            if result.complete:
                self._cleanup_unconfirmed_dom_stubs(
                    creator_id,
                    set(seen_aweme_ids),
                )
                removed_items = self.db.reconcile_video_presence(
                    creator_id,
                    seen_aweme_ids,
                    confirmation_scans=2,
                )
            else:
                self.db.add_log(
                    "warning",
                    "本次扫描尚未确认主页接口 has_more=0"
                    + (
                        f"（主页显示 {result.expected_count} 个，已捕获 {discovered_count} 个）"
                        if result.expected_count is not None
                        else f"（已捕获 {discovered_count} 个）"
                    )
                    + (f"；停止原因：{result.stop_reason}" if result.stop_reason else "")
                    + "，未用不完整结果判断作品是否删除或私密",
                    creator_id,
                )
            latest_timestamp = result.latest_create_time or max(
                (int(item["create_time"]) for item in result.videos if item.get("create_time")),
                default=None,
            )
            self.db.recount_creator(creator_id)
            self.db.add_log(
                "info",
                f"扫描发现 {discovered_count} 个作品，其中新增 {new_count} 个"
                + (f"；已排除 {result.discarded_count} 个非目标作者响应" if result.discarded_count else ""),
                creator_id,
            )
            self.db.update_scan_job(
                job_id,
                status="completed",
                ended_at=utc_now(),
                heartbeat_at=utc_now(),
                failure_reason=None,
            )
            scan_completed = True
            for removed in removed_items:
                local_state = "本地已下载，文件保留" if removed.get("status") == "downloaded" else "本地未下载"
                self.db.add_log(
                    "warning",
                    f"作品 {removed['aweme_id']} 已删除或转为私密；{local_state}",
                    creator_id,
                    int(removed["id"]),
                )
                await self._notify(
                    "作品已删除或转为私密",
                    {
                        "用户": result.nickname or creator.get("nickname") or creator_id,
                        "作品ID": removed.get("aweme_id"),
                        "文案": removed.get("description") or "无标题",
                        "作品链接": removed.get("share_url"),
                        "本地状态": local_state,
                        "本地路径": removed.get("file_path"),
                        "说明": "连续两次完整扫描未出现；本地文件不会被删除",
                    },
                    "warning",
                )

            queued_count = 0
            if new_video_ids and should_auto_download(
                scan_policy, str(claimed["job_type"])
            ):
                self.db.enqueue_download_jobs(
                    creator_id,
                    new_video_ids,
                )
                queued_count = len(new_video_ids)
                self.wake_download_workers()
            self.db.recount_creator(creator_id)
            values: dict[str, Any] = {
                "status": "idle",
                "last_scan_at": utc_now(),
                "last_error": None,
                "next_scan_at": next_scan,
            }
            if latest_timestamp:
                values["latest_publish_at"] = datetime.fromtimestamp(
                    latest_timestamp, timezone.utc
                ).isoformat(timespec="seconds")
            self.db.update_creator(creator_id, **values)
            self.db.add_log(
                "success",
                f"扫描任务完成，已加入 {queued_count} 个下载任务",
                creator_id,
            )
        except VerificationRequired as exc:
            if not scan_completed:
                self.db.update_scan_job(
                    job_id,
                    status="paused",
                    pause_requested=True,
                    ended_at=utc_now(),
                    heartbeat_at=utc_now(),
                    failure_reason=str(exc),
                )
            self.db.update_creator(
                creator_id,
                status="needs_verification",
                last_error=str(exc),
                last_scan_at=utc_now(),
                next_scan_at=next_scan,
            )
            self.db.add_log("warning", f"任务暂停：{exc}", creator_id)
            await self._notify(
                "抖音出现验证码，需要人工处理",
                {
                    "用户": creator.get("nickname") or creator_id,
                    "主页": creator.get("profile_url"),
                    "任务状态": "已暂停，不会自动绕过验证码",
                    "详细信息": str(exc),
                    "处理方式": "打开本地管理页完成验证，然后点击立即扫描",
                },
                "warning",
            )
        except asyncio.CancelledError:
            if not scan_completed:
                current = self.db.get_scan_job(job_id)
                if current["status"] not in {"completed", "failed", "cancelled"}:
                    self.db.update_scan_job(
                        job_id,
                        status="queued",
                        heartbeat_at=utc_now(),
                        failure_reason="服务关闭，任务已保留等待恢复",
                    )
            raise
        except Exception as exc:
            if not scan_completed:
                self.db.update_scan_job(
                    job_id,
                    status="failed",
                    ended_at=utc_now(),
                    heartbeat_at=utc_now(),
                    failure_reason=str(exc),
                )
            self.db.update_creator(
                creator_id,
                status="error",
                last_error=str(exc),
                last_scan_at=utc_now(),
                next_scan_at=next_scan,
            )
            self.db.add_log("error", f"扫描失败：{exc}", creator_id)
            await self._notify(
                "抖音用户扫描失败",
                {
                    "用户": creator.get("nickname") or creator_id,
                    "主页": creator.get("profile_url"),
                    "错误详情": str(exc),
                    "下次计划检查": next_scan,
                },
                "error",
            )

    async def _download_one(
        self, creator_id: int, video: dict[str, Any], *, job_id: int | None = None
    ) -> bool:
        video_id = int(video["id"])
        creator = self.db.get_creator(creator_id)
        self.db.update_video(
            video_id,
            status="downloading",
            last_error=None,
            bytes_downloaded=0,
            total_bytes=None,
        )
        try:
            content_type = video.get("content_type") or "video"
            has_assets = bool(
                candidate_image_urls(video)
                if content_type == "images"
                else video.get("video_url")
            )
            if not has_assets:
                resolved = await self.scanner.resolve_video(
                    str(video["aweme_id"]),
                    video.get("share_url"),
                    content_type,
                )
                if resolved:
                    self.db.upsert_video(creator_id, resolved)
                    video = self.db.get_video(video_id)
                    if self._is_foreign_item(resolved, creator.get("sec_uid")):
                        await self._delete_video_record(video, creator_id)
                        return
            last_progress_at = time.monotonic()
            last_progress_bytes = int(video.get("bytes_downloaded") or 0)

            def progress(current: int, total: int | None) -> None:
                nonlocal last_progress_at, last_progress_bytes
                speed: int | None = None
                now = time.monotonic()
                elapsed = now - last_progress_at
                if elapsed > 0:
                    speed = max(0, int((current - last_progress_bytes) / elapsed))
                last_progress_at = now
                last_progress_bytes = current
                if job_id is not None:
                    current_job = self.db.get_download_job(job_id)
                    if current_job["cancel_requested"]:
                        raise DownloadCancelled("用户取消下载")
                    if current_job["pause_requested"]:
                        raise DownloadPaused("用户暂停下载")
                    self.db.update_download_job(
                        job_id,
                        bytes_downloaded=current,
                        total_bytes=total,
                        speed_bytes_per_second=speed,
                        heartbeat_at=utc_now(),
                    )
                self.db.update_video(
                    video_id,
                    bytes_downloaded=current,
                    total_bytes=total,
                )
            try:
                result = await self.downloader.download(
                    creator, video, progress=progress
                )
            except DownloadControlRequested:
                raise
            except VerificationRequired:
                raise
            except Exception as first_error:
                # CDN 地址或签名过期时，从真实单视频页重新捕获媒体请求后立即重试。
                resolved = await self.scanner.resolve_video(
                    str(video["aweme_id"]),
                    video.get("share_url"),
                    video.get("content_type"),
                )
                if not resolved or not (
                    resolved.get("video_url") or resolved.get("image_urls")
                ):
                    raise first_error
                self.db.upsert_video(creator_id, resolved)
                video = self.db.get_video(video_id)
                if self._is_foreign_item(resolved, creator.get("sec_uid")):
                    await self._delete_video_record(video, creator_id)
                    return
                result = await self.downloader.download(
                    creator, video, progress=progress
                )
            self.db.update_video(
                video_id,
                status="downloaded",
                file_path=result["file_path"],
                cover_path=result["cover_path"],
                file_size=result["file_size"],
                bytes_downloaded=result["file_size"],
                total_bytes=result["file_size"],
                downloaded_at=utc_now(),
                last_error=None,
            )
            self.db.recount_creator(creator_id)
            self.db.add_log(
                "success",
                f"已下载{('图文/日常' if video.get('content_type') == 'images' else '视频')}："
                f"{video.get('description') or video['aweme_id']}",
                creator_id,
                video_id,
            )
            await self._notify(
                "抖音作品下载成功",
                {
                    "用户": creator.get("nickname") or creator_id,
                    "作品ID": video.get("aweme_id"),
                    "文案": video.get("description") or "无标题",
                    "作品链接": video.get("share_url"),
                    "文件大小（字节）": result.get("file_size"),
                    "保存路径": result.get("file_path"),
                },
                "success",
            )
            return True
        except DownloadControlRequested:
            self.db.update_video(video_id, status="pending", last_error=None)
            raise
        except VerificationRequired:
            self.db.update_video(video_id, status="pending", last_error="等待人工验证")
            raise
        except Exception as exc:
            retry_count = int(video.get("retry_count") or 0) + 1
            self.db.update_video(
                video_id,
                status="failed",
                retry_count=retry_count,
                last_error=str(exc),
            )
            self.db.add_log(
                "error",
                f"作品 {video['aweme_id']} 下载失败（第 {retry_count} 次）：{exc}",
                creator_id,
                video_id,
            )
            await self._notify(
                "作品持续下载失败" if retry_count > 1 else "抖音作品下载失败",
                {
                    "用户": self.db.get_creator(creator_id).get("nickname") or creator_id,
                    "作品ID": video.get("aweme_id"),
                    "文案": video.get("description") or "无标题",
                    "作品链接": video.get("share_url"),
                    "累计失败次数": retry_count,
                    "错误详情": str(exc),
                },
                "error",
            )
            raise

    async def _notify(self, title: str, details: dict[str, Any], level: str) -> None:
        try:
            await self.notifier.send(title, details, level)
        except Exception as exc:
            self.db.add_log("error", f"钉钉通知发送失败：{exc}")

    async def _cleanup_foreign_videos(self, creator_id: int, target_sec_uid: str) -> None:
        rows = self.db.fetch_all("SELECT * FROM videos WHERE creator_id = ?", (creator_id,))
        foreign: list[dict[str, Any]] = []
        for row in rows:
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except json.JSONDecodeError:
                continue
            author = raw.get("author") if isinstance(raw, dict) else None
            author = author if isinstance(author, dict) else {}
            sec_uid = author.get("sec_uid") or author.get("secUid")
            if sec_uid and sec_uid != target_sec_uid:
                foreign.append(row)
        if not foreign:
            return

        download_root = self.settings.download_dir.resolve()
        for row in foreign:
            await self._delete_video_record(
                row, creator_id, download_root, refresh_metadata=False
            )
        await self._rewrite_metadata(creator_id)
        self.db.recount_creator(creator_id)

    def _cleanup_unconfirmed_dom_stubs(
        self, creator_id: int, seen_aweme_ids: set[str]
    ) -> None:
        """Remove recommendation links created by the old DOM fallback.

        Only empty, never-downloaded records are eligible. Real works with captured JSON or a
        local file continue through the normal two-complete-scan missing confirmation flow.
        """
        rows = self.db.fetch_all(
            """
            SELECT * FROM videos
            WHERE creator_id = ? AND status IN ('pending', 'failed') AND file_path IS NULL
            """,
            (creator_id,),
        )
        for row in rows:
            if str(row["aweme_id"]) in seen_aweme_ids:
                continue
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except json.JSONDecodeError:
                continue
            if raw:
                continue
            self.db.execute("DELETE FROM videos WHERE id = ?", (int(row["id"]),))
            self.db.add_log(
                "warning",
                f"已清理未被主页接口确认的推荐候选 {row['aweme_id']}",
                creator_id,
            )

    @staticmethod
    def _is_foreign_item(item: dict[str, Any], target_sec_uid: str | None) -> bool:
        sec_uid = item.get("sec_uid")
        if not sec_uid:
            raw = item.get("raw")
            author = raw.get("author") if isinstance(raw, dict) else None
            if isinstance(author, dict):
                sec_uid = author.get("sec_uid") or author.get("secUid")
        return bool(target_sec_uid and sec_uid and sec_uid != target_sec_uid)

    async def _delete_video_record(
        self,
        row: dict[str, Any],
        creator_id: int,
        download_root: Path | None = None,
        refresh_metadata: bool = True,
    ) -> None:
        download_root = download_root or self.settings.download_dir.resolve()
        for key in ("file_path", "cover_path"):
            value = row.get(key)
            if not value:
                continue
            path = Path(value).resolve()
            if download_root in path.parents and path.is_file():
                path.unlink()
        self.db.execute("DELETE FROM videos WHERE id = ?", (int(row["id"]),))
        self.db.add_log(
            "warning",
            f"已清理误收的其他作者作品 {row['aweme_id']}",
            creator_id,
        )
        if refresh_metadata:
            await self._rewrite_metadata(creator_id)
            self.db.recount_creator(creator_id)

    async def _rewrite_metadata(self, creator_id: int) -> None:
        creator = self.db.get_creator(creator_id)
        creator_dir = self.settings.download_dir / safe_name(
            f"{creator.get('nickname') or '未知用户'}_{creator.get('sec_uid') or creator_id}",
            f"creator_{creator_id}",
            120,
        )
        metadata_path = creator_dir / "metadata.jsonl"
        if not metadata_path.exists():
            return
        rows = self.db.fetch_all(
            """
            SELECT aweme_id, description, create_time, share_url, content_type,
                   asset_count, is_daily, file_path, downloaded_at
            FROM videos WHERE creator_id = ? AND status = 'downloaded'
            ORDER BY COALESCE(create_time, 0), id
            """,
            (creator_id,),
        )
        temp_path = metadata_path.with_suffix(".jsonl.tmp")
        content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, metadata_path)

    async def shutdown(self) -> None:
        self._download_stopping.set()
        self._download_wakeup.set()
        running = [task for task in self._tasks.values() if not task.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        preview_tasks = [task for task in self._preview_tasks.values() if not task.done()]
        for task in preview_tasks:
            task.cancel()
        if preview_tasks:
            await asyncio.gather(*preview_tasks, return_exceptions=True)
        download_tasks = [task for task in self._download_tasks if not task.done()]
        if download_tasks:
            await asyncio.gather(*download_tasks, return_exceptions=True)
        self._download_tasks.clear()

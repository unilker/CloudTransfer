"""Job execution: drives the planner, then the relay, file by file.

Concurrency is bounded by ``CT_MAX_PARALLEL_FILES`` because peak memory is
roughly ``chunk_size * parallel_files``, and because both providers throttle
per account rather than per connection - more parallelism past a point just
earns more 429s.

Stopping is cooperative. The progress callback raises :class:`JobStopped`
between chunks, which unwinds the relay but leaves the OneDrive upload session
intact. Graph remembers the offset, so resuming continues from the exact byte
it already has rather than re-sending the file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import func, select

from ..accounts import TokenManager
from ..config import Settings, get_settings
from ..db import session_scope
from ..errors import AuthError, CloudTransferError, ProviderError, SkipFile
from ..http import build_client
from ..models import ConflictPolicy, FileTask, Job, JobStatus, TaskStatus
from ..naming import join_path
from ..providers.gdrive import GoogleDriveSource
from ..providers.onedrive import OneDriveDest
from .planner import plan_job
from .transfer import FileTransfer

log = logging.getLogger(__name__)

# How often a running file's byte count is written to SQLite.
PROGRESS_FLUSH_SECONDS = 1.0


class JobStopped(CloudTransferError):
    """Raised inside the relay to unwind a paused or cancelled job."""


@dataclass
class LiveProgress:
    """In-memory snapshot, so the UI can poll without hitting the database."""

    job_id: int
    files: dict[int, tuple[str, int, int]] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    bytes_at_start: int = 0
    bytes_now: int = 0

    @property
    def rate_bytes_per_second(self) -> float:
        elapsed = time.monotonic() - self.started_at
        if elapsed < 1.0:
            return 0.0
        return max(0.0, (self.bytes_now - self.bytes_at_start) / elapsed)


class JobRunner:
    """Owns the running asyncio tasks for all active jobs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._tasks: dict[int, asyncio.Task] = {}
        self._stops: dict[int, asyncio.Event] = {}
        self._cancelling: set[int] = set()
        self._progress: dict[int, LiveProgress] = {}

    # -- control ------------------------------------------------------------

    def is_running(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    def progress(self, job_id: int) -> LiveProgress | None:
        return self._progress.get(job_id)

    async def start(self, job_id: int) -> None:
        if self.is_running(job_id):
            return
        stop = asyncio.Event()
        self._stops[job_id] = stop
        self._cancelling.discard(job_id)
        self._progress[job_id] = LiveProgress(job_id=job_id)
        task = asyncio.create_task(self._guarded_run(job_id), name=f"job-{job_id}")
        self._tasks[job_id] = task

    async def pause(self, job_id: int) -> bool:
        """Stop after the current chunk, leaving everything resumable."""
        stop = self._stops.get(job_id)
        if stop is None or not self.is_running(job_id):
            return False
        stop.set()
        return True

    async def cancel(self, job_id: int) -> bool:
        """Stop and mark the job cancelled."""
        self._cancelling.add(job_id)
        stop = self._stops.get(job_id)
        if stop is not None:
            stop.set()
        if not self.is_running(job_id):
            async with session_scope(self.settings) as session:
                job = await session.get(Job, job_id)
                if job is not None and job.status not in JobStatus.TERMINAL:
                    job.status = JobStatus.CANCELLED
                    job.finished_at = func.now()
            return True
        return True

    async def shutdown(self) -> None:
        for stop in self._stops.values():
            stop.set()
        running = [t for t in self._tasks.values() if not t.done()]
        if running:
            await asyncio.wait(running, timeout=30)
        for task in running:
            if not task.done():  # pragma: no cover - slow provider
                task.cancel()

    # -- execution ----------------------------------------------------------

    async def _guarded_run(self, job_id: int) -> None:
        try:
            await self._run(job_id)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:
            log.exception("job %d failed", job_id)
            async with session_scope(self.settings) as session:
                job = await session.get(Job, job_id)
                if job is not None:
                    job.status = JobStatus.FAILED
                    job.error = str(exc)
                    job.finished_at = func.now()
        finally:
            self._stops.pop(job_id, None)
            self._cancelling.discard(job_id)

    async def _run(self, job_id: int) -> None:
        stop = self._stops[job_id]
        live = self._progress[job_id]

        google = TokenManager("google", self.settings)
        microsoft = TokenManager("microsoft", self.settings)

        async with build_client() as client:
            source = GoogleDriveSource(google.get_token, self.settings, client=client)
            dest = OneDriveDest(microsoft.get_token, self.settings, client=client)

            async with session_scope(self.settings) as session:
                job = await session.get(Job, job_id)
                if job is None:
                    raise ValueError(f"job {job_id} does not exist")
                pending_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(FileTask)
                        .where(FileTask.job_id == job_id)
                    )
                ).scalar_one()

            if pending_count == 0:
                await plan_job(job_id, source, self.settings)

            async with session_scope(self.settings) as session:
                job = await session.get(Job, job_id)
                if job is None:  # pragma: no cover
                    raise ValueError(f"job {job_id} disappeared")
                job.status = JobStatus.RUNNING
                if job.started_at is None:
                    job.started_at = func.now()
                conflict = job.conflict_policy
                verify = job.verify and self.settings.verify_hashes
                dest_root = job.dest_folder_id
                live.bytes_at_start = job.done_bytes
                live.bytes_now = job.done_bytes

            folder_cache: dict[str, str] = {"": dest_root}
            folder_lock = asyncio.Lock()
            semaphore = asyncio.Semaphore(self.settings.max_parallel_files)

            async def resolve_folder(rel_path: str) -> str:
                """Create the destination folder chain once, on first use.

                Walks down from the deepest ancestor already in the cache. Kept
                iterative on purpose: recursing while holding ``folder_lock``
                would deadlock, since an asyncio lock is not reentrant.
                """
                if rel_path in folder_cache:
                    return folder_cache[rel_path]
                async with folder_lock:
                    if rel_path in folder_cache:
                        return folder_cache[rel_path]
                    parent_id = folder_cache[""]
                    walked = ""
                    for part in rel_path.split("/"):
                        walked = f"{walked}/{part}" if walked else part
                        cached = folder_cache.get(walked)
                        if cached is not None:
                            parent_id = cached
                            continue
                        folder = await dest.ensure_folder(parent_id, part)
                        folder_cache[walked] = folder.id
                        parent_id = folder.id
                    return parent_id

            while not stop.is_set():
                async with session_scope(self.settings) as session:
                    batch = list(
                        (
                            await session.execute(
                                select(FileTask)
                                .where(
                                    FileTask.job_id == job_id,
                                    FileTask.status.in_(
                                        [TaskStatus.PENDING, TaskStatus.RUNNING]
                                    ),
                                )
                                .order_by(FileTask.rel_path, FileTask.id)
                                .limit(self.settings.max_parallel_files * 4)
                            )
                        )
                        .scalars()
                        .all()
                    )
                if not batch:
                    break

                await asyncio.gather(
                    *(
                        self._run_one(
                            task_id=task.id,
                            job_id=job_id,
                            source=source,
                            dest=dest,
                            resolve_folder=resolve_folder,
                            semaphore=semaphore,
                            stop=stop,
                            conflict=conflict,
                            verify=verify,
                            live=live,
                        )
                        for task in batch
                    )
                )

            await self._finalize(job_id, stop)

            await source.close()
            await dest.close()

    async def _run_one(
        self,
        *,
        task_id: int,
        job_id: int,
        source: GoogleDriveSource,
        dest: OneDriveDest,
        resolve_folder,
        semaphore: asyncio.Semaphore,
        stop: asyncio.Event,
        conflict: str,
        verify: bool,
        live: LiveProgress,
    ) -> None:
        if stop.is_set():
            return

        async with semaphore:
            if stop.is_set():
                return

            async with session_scope(self.settings) as session:
                task = await session.get(FileTask, task_id)
                if task is None or task.status not in (
                    TaskStatus.PENDING,
                    TaskStatus.RUNNING,
                ):
                    return
                task.status = TaskStatus.RUNNING
                task.attempts += 1
                task.started_at = task.started_at or func.now()
                snapshot = {
                    "source_id": task.source_id,
                    "source_name": task.source_name,
                    "rel_path": task.rel_path,
                    "dest_name": task.dest_name,
                    "export_mime": task.export_mime,
                    "upload_url": task.upload_url,
                    "size": task.source_size,
                    "attempts": task.attempts,
                }

            label = join_path(snapshot["rel_path"], snapshot["dest_name"])
            last_flush = 0.0
            baseline = live.bytes_now

            async def on_progress(done: int, total: int) -> None:
                nonlocal last_flush
                if stop.is_set():
                    raise JobStopped(f"stopped while transferring {label!r}")
                live.files[task_id] = (label, done, total)
                live.bytes_now = baseline + done
                now = time.monotonic()
                if now - last_flush >= PROGRESS_FLUSH_SECONDS:
                    last_flush = now
                    async with session_scope(self.settings) as session:
                        row = await session.get(FileTask, task_id)
                        if row is not None:
                            row.bytes_done = done

            async def on_session(url: str) -> None:
                async with session_scope(self.settings) as session:
                    row = await session.get(FileTask, task_id)
                    if row is not None and row.upload_url != url:
                        row.upload_url = url

            try:
                item = await source.get_item(snapshot["source_id"])
                if item.is_shortcut:
                    item = await source.resolve_shortcut(item)

                parent_id = await resolve_folder(snapshot["rel_path"])

                if conflict == ConflictPolicy.SKIP:
                    existing = await dest.find_child(parent_id, snapshot["dest_name"])
                    if existing is not None:
                        await self._settle(
                            task_id,
                            job_id,
                            status=TaskStatus.SKIPPED,
                            error=(
                                "a file with this name already exists in the "
                                "destination and the conflict policy is 'skip'"
                            ),
                        )
                        return

                graph_conflict = (
                    "replace" if conflict == ConflictPolicy.REPLACE else "rename"
                )

                transfer = FileTransfer(
                    source,
                    dest,
                    self.settings,
                    progress=on_progress,
                    on_session=on_session,
                )
                result = await transfer.transfer(
                    item,
                    dest_parent_id=parent_id,
                    dest_name=snapshot["dest_name"],
                    export_mime=snapshot["export_mime"],
                    conflict=graph_conflict,
                    resume_url=snapshot["upload_url"],
                    verify=verify,
                )

                await self._settle(
                    task_id,
                    job_id,
                    status=TaskStatus.DONE,
                    bytes_done=result.bytes_moved,
                    dest_item_id=result.item.id if result.item else None,
                    verified=result.verified,
                    hash_note=result.hash_note,
                    clear_session=True,
                )
                live.files.pop(task_id, None)
                log.info(
                    "moved %s (%d bytes, %s%s)",
                    label,
                    result.bytes_moved,
                    result.strategy,
                    ", resumed" if result.resumed else "",
                )

            except JobStopped:
                # Leave the task pending with its session URL stored.
                async with session_scope(self.settings) as session:
                    row = await session.get(FileTask, task_id)
                    if row is not None:
                        row.status = TaskStatus.PENDING
                live.files.pop(task_id, None)
                log.info("paused during %s; it will resume from where it stopped", label)

            except SkipFile as exc:
                await self._settle(
                    task_id, job_id, status=TaskStatus.SKIPPED, error=str(exc)
                )
                live.files.pop(task_id, None)
                log.info("skipped %s: %s", label, exc)

            except AuthError:
                raise

            except (ProviderError, CloudTransferError) as exc:
                await self._settle(
                    task_id, job_id, status=TaskStatus.FAILED, error=str(exc)
                )
                live.files.pop(task_id, None)
                log.warning("failed %s: %s", label, exc)

    async def _settle(
        self,
        task_id: int,
        job_id: int,
        *,
        status: str,
        bytes_done: int | None = None,
        dest_item_id: str | None = None,
        verified: bool | None = None,
        hash_note: str | None = None,
        error: str | None = None,
        clear_session: bool = False,
    ) -> None:
        """Record a task's outcome and roll it into the job's counters."""
        async with session_scope(self.settings) as session:
            task = await session.get(FileTask, task_id)
            if task is None:  # pragma: no cover - deleted mid-flight
                return
            task.status = status
            task.finished_at = func.now()
            if bytes_done is not None:
                task.bytes_done = bytes_done
            if dest_item_id is not None:
                task.dest_item_id = dest_item_id
            if verified is not None:
                task.verified = verified
            if hash_note is not None:
                task.hash_note = hash_note
            task.error = error
            if clear_session:
                task.upload_url = None
                task.upload_expires_at = None

            job = await session.get(Job, job_id)
            if job is None:  # pragma: no cover
                return
            counts = dict(
                (
                    await session.execute(
                        select(FileTask.status, func.count())
                        .where(FileTask.job_id == job_id)
                        .group_by(FileTask.status)
                    )
                ).all()
            )
            moved = (
                await session.execute(
                    select(func.coalesce(func.sum(FileTask.bytes_done), 0)).where(
                        FileTask.job_id == job_id, FileTask.status == TaskStatus.DONE
                    )
                )
            ).scalar_one()
            job.done_files = counts.get(TaskStatus.DONE, 0)
            job.failed_files = counts.get(TaskStatus.FAILED, 0)
            job.skipped_files = counts.get(TaskStatus.SKIPPED, 0)
            job.done_bytes = int(moved or 0)

    async def _finalize(self, job_id: int, stop: asyncio.Event) -> None:
        cancelled = job_id in self._cancelling
        async with session_scope(self.settings) as session:
            job = await session.get(Job, job_id)
            if job is None:  # pragma: no cover
                return
            remaining = (
                await session.execute(
                    select(func.count())
                    .select_from(FileTask)
                    .where(
                        FileTask.job_id == job_id,
                        FileTask.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
                    )
                )
            ).scalar_one()

            if cancelled:
                job.status = JobStatus.CANCELLED
                job.finished_at = func.now()
            elif stop.is_set() and remaining:
                job.status = JobStatus.PAUSED
            elif remaining:  # pragma: no cover - loop exits when none remain
                job.status = JobStatus.PAUSED
            elif job.failed_files:
                job.status = JobStatus.COMPLETED_WITH_ERRORS
                job.finished_at = func.now()
            else:
                job.status = JobStatus.COMPLETED
                job.finished_at = func.now()
            log.info(
                "job %d finished as %s (%d done, %d failed, %d skipped)",
                job_id, job.status, job.done_files, job.failed_files, job.skipped_files,
            )


_runner: JobRunner | None = None


def get_runner(settings: Settings | None = None) -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner(settings)
    return _runner


async def reset_stale_jobs(settings: Settings | None = None) -> int:
    """On startup, demote jobs the previous process left mid-flight.

    Their file tasks keep their stored upload sessions, so pressing resume
    continues each file from the byte OneDrive already has.
    """
    settings = settings or get_settings()
    async with session_scope(settings) as session:
        jobs = list(
            (
                await session.execute(
                    select(Job).where(
                        Job.status.in_(
                            [JobStatus.RUNNING, JobStatus.PLANNING, JobStatus.PENDING]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            job.status = JobStatus.PAUSED
        running_tasks = list(
            (
                await session.execute(
                    select(FileTask).where(FileTask.status == TaskStatus.RUNNING)
                )
            )
            .scalars()
            .all()
        )
        for task in running_tasks:
            task.status = TaskStatus.PENDING
    if jobs:
        log.info("reset %d job(s) left running by a previous process to paused", len(jobs))
    return len(jobs)

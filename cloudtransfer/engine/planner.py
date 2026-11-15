"""Turn a Drive folder into a concrete list of file tasks.

Planning is a separate pass from transferring so that the job's real size is
known before a single byte moves, and so every rename is decided once and
recorded. The naming work has to happen here rather than file by file, because
uniqueness is a property of a whole destination folder:

* Drive allows two siblings with the same name; OneDrive does not.
* Sanitizing can collide two distinct names (``a:b`` and ``a*b`` both become
  ``a_b``).
* Folders and files share one namespace on OneDrive, so a folder named
  ``report`` and a file named ``report`` conflict with each other.

So each folder's children are sanitized and de-duplicated as one set, with the
export extension already applied, before anything is written down.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select

from ..config import Settings, get_settings
from ..db import session_scope
from ..errors import SkipFile
from ..models import Direction, FileTask, Job, JobStatus, TaskStatus
from ..naming import (
    POLICIES,
    NamingPolicy,
    check_path_length,
    dedupe_name,
    join_path,
    sanitize_name,
)
from ..providers.base import SourceProvider

log = logging.getLogger(__name__)

# Flush planned tasks to SQLite in batches rather than one statement per file.
BATCH_SIZE = 200


class PlannedFile:
    """One file decided on during planning, before it reaches the database."""

    __slots__ = (
        "source_id", "source_name", "source_mime", "source_size", "source_md5",
        "source_sha256", "export_mime", "rel_path", "dest_name", "notes",
        "skip_reason", "size_is_estimate",
    )

    def __init__(self, **kw) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))


async def _collect(
    source: SourceProvider,
    root_id: str,
    *,
    include_trashed: bool,
    policy: NamingPolicy,
) -> list[PlannedFile]:
    """Walk the source tree, resolving names and export formats as we go."""
    planned: list[PlannedFile] = []
    visited: set[str] = {root_id}
    queue: list[tuple[str, str]] = [(root_id, "")]

    while queue:
        folder_id, rel_path = queue.pop(0)
        children = await source.list_children(folder_id, include_trashed=include_trashed)

        # Folders and files compete for the same names on OneDrive, so one
        # taken-set covers both.
        taken: set[str] = set()
        folders: list[tuple[str, str]] = []

        for child in children:
            original = child

            if child.is_shortcut:
                try:
                    child = await source.resolve_shortcut(child)
                except SkipFile as exc:
                    planned.append(
                        PlannedFile(
                            source_id=original.id,
                            source_name=original.name,
                            source_mime=original.mime_type,
                            source_size=0,
                            rel_path=rel_path,
                            dest_name=original.name,
                            notes=[],
                            skip_reason=str(exc),
                        )
                    )
                    continue

            if child.is_folder:
                safe, reasons = sanitize_name(child.name, policy, fallback="folder")
                safe, renamed = dedupe_name(safe, taken, policy)
                if renamed:
                    reasons.append("renamed to avoid a name already used in this folder")
                if reasons:
                    log.info("folder %r -> %r (%s)", child.name, safe, "; ".join(reasons))
                if child.id in visited:
                    log.warning("folder loop at %r; not descending twice", child.name)
                    continue
                visited.add(child.id)
                folders.append((child.id, join_path(rel_path, safe)))
                continue

            # Files: work out the final name (extension included) first.
            notes: list[str] = []
            skip_reason: str | None = None
            export_mime: str | None = None
            base_name = child.name

            try:
                base_name, export_mime = source.plan_transfer_name(child)
                if export_mime:
                    notes.append(f"converted from {child.mime_type} to keep it editable")
            except SkipFile as exc:
                skip_reason = str(exc)

            safe, reasons = sanitize_name(base_name, policy)
            notes.extend(reasons)
            safe, renamed = dedupe_name(safe, taken, policy)
            if renamed:
                notes.append("renamed to avoid a name already used in this folder")

            size = child.size or 0
            if skip_reason is None:
                if not child.can_download:
                    skip_reason = "this account is not allowed to download the file"
                elif size > policy.max_file_size:
                    skip_reason = (
                        f"{size} bytes exceeds {policy.provider}'s "
                        f"{policy.max_file_size_label} per-file limit"
                    )

            warning = check_path_length(join_path(rel_path, safe), policy)
            if warning:
                notes.append(warning)
                if skip_reason is None:
                    skip_reason = warning

            planned.append(
                PlannedFile(
                    source_id=child.id,
                    source_name=child.name,
                    source_mime=child.mime_type,
                    source_size=size,
                    source_md5=child.md5,
                    source_sha256=child.sha256,
                    export_mime=export_mime,
                    rel_path=rel_path,
                    dest_name=safe,
                    notes=notes,
                    skip_reason=skip_reason,
                    # A converted document's final size is unknown until the
                    # export arrives, so what is recorded now is a guess.
                    size_is_estimate=bool(export_mime) or child.size is None,
                )
            )

        queue.extend(folders)

    return planned


async def plan_job(
    job_id: int,
    source: SourceProvider,
    settings: Settings | None = None,
) -> int:
    """Populate a job's file tasks and totals. Returns the number of files.

    Re-planning a job replaces any tasks that have not been transferred yet
    and keeps the finished ones, so a re-run only does the outstanding work.
    """
    settings = settings or get_settings()

    async with session_scope(settings) as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise ValueError(f"job {job_id} does not exist")
        job.status = JobStatus.PLANNING
        root_id = job.source_folder_id
        include_trashed = job.include_trashed
        direction = job.direction

    # Names have to satisfy the destination, which depends on the direction.
    _, dest_side = Direction.SIDES.get(direction, ("gdrive", "onedrive"))
    policy = POLICIES[dest_side]

    log.info(
        "planning job %d (%s) from folder %s, naming for %s",
        job_id, direction, root_id, policy.provider,
    )
    planned = await _collect(
        source, root_id, include_trashed=include_trashed, policy=policy
    )

    async with session_scope(settings) as session:
        job = await session.get(Job, job_id)
        if job is None:  # pragma: no cover - deleted mid-plan
            raise ValueError(f"job {job_id} disappeared while planning")

        # Keep what already transferred; discard stale pending rows.
        done_rows = (
            await session.execute(
                select(FileTask.source_id, FileTask.source_size).where(
                    FileTask.job_id == job_id, FileTask.status == TaskStatus.DONE
                )
            )
        ).all()
        done_ids = {row[0] for row in done_rows}
        done_count = len(done_rows)
        done_bytes = sum(row[1] or 0 for row in done_rows)
        await session.execute(
            delete(FileTask).where(
                FileTask.job_id == job_id, FileTask.status != TaskStatus.DONE
            )
        )

        total_files = 0
        total_bytes = 0
        skipped = 0

        for index, entry in enumerate(planned):
            if entry.source_id in done_ids:
                continue
            task = FileTask(
                job_id=job_id,
                status=TaskStatus.SKIPPED if entry.skip_reason else TaskStatus.PENDING,
                source_id=entry.source_id,
                source_name=entry.source_name or "",
                source_mime=entry.source_mime or "",
                source_size=entry.source_size or 0,
                source_md5=entry.source_md5,
                source_sha256=entry.source_sha256,
                export_mime=entry.export_mime,
                size_is_estimate=bool(entry.size_is_estimate),
                rel_path=entry.rel_path or "",
                dest_name=entry.dest_name or "",
                rename_notes=entry.notes or None,
                error=entry.skip_reason,
            )
            session.add(task)
            if entry.skip_reason:
                skipped += 1
            else:
                total_files += 1
                total_bytes += entry.source_size or 0
            if index % BATCH_SIZE == BATCH_SIZE - 1:
                await session.flush()

        job.total_files = total_files + done_count
        job.total_bytes = total_bytes + done_bytes
        job.done_files = done_count
        job.done_bytes = done_bytes
        job.skipped_files = skipped
        job.failed_files = 0
        job.status = JobStatus.PENDING
        job.error = None

    log.info(
        "job %d planned: %d files to move (%d bytes), %d skipped, %d already done",
        job_id, total_files, total_bytes, skipped, done_count,
    )
    return total_files

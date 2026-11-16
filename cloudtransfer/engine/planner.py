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
from collections.abc import AsyncIterator, Callable

from sqlalchemy import delete, func, select

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
from .totals import refresh_totals

log = logging.getLogger(__name__)

class PlanningInterrupted(Exception):
    """The walk was stopped before it finished.

    Counting a large tree is many round trips, and a user who presses pause
    should not have to wait for all of them. Nothing is written when this is
    raised: a partial plan would look complete and quietly transfer only some
    of the files, so a resume re-counts from the start instead.
    """


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


async def _walk(
    source: SourceProvider,
    root_id: str,
    *,
    include_trashed: bool,
    policy: NamingPolicy,
    should_stop: Callable[[], bool] | None = None,
    selection: set[str] | None = None,
) -> AsyncIterator[list[PlannedFile]]:
    """Walk the source tree, yielding each folder's files as that folder is read.

    Yielding per folder rather than returning one list at the end is what lets
    a job transfer while it is still counting. It is safe precisely because
    uniqueness is decided *within* a folder and never across folders: once a
    folder has been listed, its children's names are final and nothing read
    later can change them.

    ``selection`` narrows the top level to the items the user ticked. It
    applies only there: a ticked folder is taken whole, which is what ticking
    a folder means.
    """
    visited: set[str] = {root_id}
    queue: list[tuple[str, str]] = [(root_id, "")]
    produced = 0

    while queue:
        # Checked once per folder, which is once per network round trip - the
        # finest granularity available without splitting a single listing.
        if should_stop is not None and should_stop():
            raise PlanningInterrupted(
                f"stopped after {produced} files, with {len(queue)} folders left"
            )
        folder_id, rel_path = queue.pop(0)
        children = await source.list_children(folder_id, include_trashed=include_trashed)

        if selection and folder_id == root_id:
            # Matched before shortcuts are resolved, because the listing the
            # user ticked showed the shortcut, not its target.
            children = [child for child in children if child.id in selection]

        # Folders and files compete for the same names on OneDrive, so one
        # taken-set covers both.
        taken: set[str] = set()
        folders: list[tuple[str, str]] = []
        batch: list[PlannedFile] = []

        for child in children:
            original = child

            if child.is_shortcut:
                try:
                    child = await source.resolve_shortcut(child)
                except SkipFile as exc:
                    batch.append(
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

            batch.append(
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
        produced += len(batch)
        if batch:
            yield batch


async def plan_job(
    job_id: int,
    source: SourceProvider,
    settings: Settings | None = None,
    should_stop: Callable[[], bool] | None = None,
    *,
    manage_status: bool = True,
) -> int:
    """Populate a job's file tasks and totals. Returns the number of files to move.

    Tasks are written folder by folder as the walk proceeds, so the worker can
    start transferring within seconds of a job starting instead of waiting for
    a whole tree to be counted. ``Job.plan_complete`` says whether the totals
    have stopped growing.

    Re-planning a finished plan replaces any tasks that have not transferred
    yet and keeps the finished ones, so a re-run only does the outstanding
    work. Continuing an *unfinished* plan keeps what was already written and
    adds to it, so a stopped count is never thrown away.

    ``manage_status`` must be False when a worker is transferring at the same
    time. Two writers racing on one status field is how a running job ends up
    labelled "pending": the worker owns it in that case, and this only
    maintains its own ``plan_complete``.
    """
    settings = settings or get_settings()

    async with session_scope(settings) as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise ValueError(f"job {job_id} does not exist")

        resuming = not job.plan_complete
        if not resuming:
            # A genuine re-plan: keep what transferred, discard stale rows.
            await session.execute(
                delete(FileTask).where(
                    FileTask.job_id == job_id, FileTask.status != TaskStatus.DONE
                )
            )

        if manage_status:
            job.status = JobStatus.PLANNING
        job.plan_complete = False
        job.error = None
        root_id = job.source_folder_id
        include_trashed = job.include_trashed
        direction = job.direction
        selection = set(job.selection or [])

    # Start the running totals from whatever survived above, so a resumed plan
    # counts on from where it stopped rather than from zero.
    async with session_scope(settings) as session:
        # Keyed on where the file lands, not on the file: a shortcut and its
        # target share a source id, and both are real transfers under their
        # own names. Only an identical row is a duplicate.
        existing_keys = {
            (row[0], row[1], row[2])
            for row in (
                await session.execute(
                    select(
                        FileTask.source_id, FileTask.rel_path, FileTask.dest_name
                    ).where(FileTask.job_id == job_id)
                )
            ).all()
        }
        rows = (
            await session.execute(
                select(
                    FileTask.status,
                    func.count(),
                    func.coalesce(func.sum(FileTask.source_size), 0),
                )
                .where(FileTask.job_id == job_id)
                .group_by(FileTask.status)
            )
        ).all()

    by_status = {row[0]: (row[1], row[2]) for row in rows}
    done_count, done_bytes = by_status.get(TaskStatus.DONE, (0, 0))
    skipped = by_status.get(TaskStatus.SKIPPED, (0, 0))[0]
    total_files = sum(
        count for status, (count, _) in by_status.items()
        if status not in (TaskStatus.SKIPPED,)
    )
    total_bytes = sum(
        size for status, (_, size) in by_status.items()
        if status not in (TaskStatus.SKIPPED,)
    )

    # Names have to satisfy the destination, which depends on the direction.
    _, dest_side = Direction.SIDES.get(direction, ("gdrive", "onedrive"))
    policy = POLICIES[dest_side]

    log.info(
        "planning job %d (%s) from folder %s (%s), naming for %s%s",
        job_id,
        direction,
        root_id,
        f"{len(selection)} selected items" if selection else "whole folder",
        policy.provider,
        " (continuing an unfinished count)" if resuming and existing_keys else "",
    )

    async def _store(batch: list[PlannedFile]) -> None:
        """Write one folder's files and refresh the job's running totals."""
        nonlocal total_files, total_bytes, skipped
        async with session_scope(settings) as session:
            for entry in batch:
                key = (entry.source_id, entry.rel_path or "", entry.dest_name or "")
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                session.add(
                    FileTask(
                        job_id=job_id,
                        status=(
                            TaskStatus.SKIPPED if entry.skip_reason else TaskStatus.PENDING
                        ),
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
                )
                if entry.skip_reason:
                    skipped += 1
                else:
                    total_files += 1
                    total_bytes += entry.source_size or 0

            job = await session.get(Job, job_id)
            if job is not None:
                job.total_files = total_files
                job.total_bytes = total_bytes
                job.skipped_files = skipped

    try:
        async for batch in _walk(
            source,
            root_id,
            include_trashed=include_trashed,
            policy=policy,
            should_stop=should_stop,
            selection=selection or None,
        ):
            await _store(batch)
    except PlanningInterrupted:
        # What was written stays: it is a correct plan of the part of the tree
        # that was read, and the rows let a resume carry on transferring at
        # once. plan_complete stays False, which is what says "not all of it".
        log.info("job %d: counting stopped with %d files found so far", job_id, total_files)
        raise

    async with session_scope(settings) as session:
        job = await session.get(Job, job_id)
        if job is None:  # pragma: no cover - deleted mid-plan
            raise ValueError(f"job {job_id} disappeared while planning")
        job.total_files = total_files
        job.total_bytes = total_bytes
        job.plan_complete = True
        # Derived from the rows as they are now, never from the figures read
        # when this plan started: transferring runs alongside counting, so
        # those are already stale by the time the walk ends.
        await refresh_totals(session, job_id)
        if manage_status and job.status == JobStatus.PLANNING:
            job.status = JobStatus.PENDING

    log.info(
        "job %d planned: %d files to move (%d bytes), %d skipped, %d already done",
        job_id, total_files - done_count, total_bytes - done_bytes, skipped, done_count,
    )
    return total_files - done_count

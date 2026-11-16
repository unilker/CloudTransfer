"""Recomputing a job's totals from its file tasks.

Both the planner and the worker need this, and from opposite directions: the
planner has just added rows, the worker has just settled one. Neither can own
it without the other importing it, so it lives here.
"""

from __future__ import annotations

from sqlalchemy import func, select

from ..models import FileTask, Job, TaskStatus


async def refresh_totals(session, job_id: int) -> None:
    """Refresh a job's progress figures from its file tasks.

    Derived from the rows every time rather than accumulated, so the figures
    cannot drift - which matters now that counting and transferring run at the
    same time and both touch the same job.
    """
    job = await session.get(Job, job_id)
    if job is None:  # pragma: no cover - deleted mid-flight
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
    # Every byte the destination holds, not only those in finished files. A
    # paused job would otherwise report nothing moved, when in fact the
    # destination is keeping the partial uploads and a resume continues from
    # them.
    moved = (
        await session.execute(
            select(func.coalesce(func.sum(FileTask.bytes_done), 0)).where(
                FileTask.job_id == job_id,
                FileTask.status != TaskStatus.SKIPPED,
            )
        )
    ).scalar_one()

    job.done_files = counts.get(TaskStatus.DONE, 0)
    job.failed_files = counts.get(TaskStatus.FAILED, 0)
    job.skipped_files = counts.get(TaskStatus.SKIPPED, 0)
    job.done_bytes = int(moved or 0)

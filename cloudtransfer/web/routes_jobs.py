"""Job creation, control, progress and reporting."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from ..accounts import load_account
from ..config import get_settings
from ..db import session_scope
from ..engine.worker import build_providers, get_runner
from ..errors import ProviderError
from ..http import build_client
from ..models import ConflictPolicy, Direction, FileTask, Job, JobStatus, TaskStatus
from ..naming import join_path
from ..oauth import google_can_write
from .deps import require_account

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs")

SSE_POLL_SECONDS = 1.0


class JobCreate(BaseModel):
    direction: Literal["gdrive_to_onedrive", "onedrive_to_gdrive"] = (
        Direction.GDRIVE_TO_ONEDRIVE
    )
    source_folder_id: str = Field(..., min_length=1)
    source_folder_name: str = ""
    dest_folder_id: str = Field(..., min_length=1)
    dest_folder_path: str = ""
    conflict_policy: Literal["rename", "replace", "skip"] = ConflictPolicy.RENAME
    verify: bool = True
    include_trashed: bool = False
    start_immediately: bool = True


def _job_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "direction": job.direction,
        "direction_label": job.direction_label,
        "source_folder_id": job.source_folder_id,
        "source_folder_name": job.source_folder_name,
        "dest_folder_id": job.dest_folder_id,
        "dest_folder_path": job.dest_folder_path,
        "conflict_policy": job.conflict_policy,
        "verify": job.verify,
        "total_files": job.total_files,
        "total_bytes": job.total_bytes,
        "done_files": job.done_files,
        "done_bytes": job.done_bytes,
        "failed_files": job.failed_files,
        "skipped_files": job.skipped_files,
        "percent": round(job.percent, 2),
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _task_dict(task: FileTask) -> dict:
    return {
        "id": task.id,
        "status": task.status,
        "source_name": task.source_name,
        "source_mime": task.source_mime,
        "source_size": task.source_size,
        "size_is_estimate": task.size_is_estimate,
        "rel_path": task.rel_path,
        "dest_name": task.dest_name,
        "dest_path": join_path(task.rel_path, task.dest_name),
        "renamed": task.dest_name != task.source_name,
        "rename_notes": task.rename_notes or [],
        "bytes_done": task.bytes_done,
        "percent": round(task.percent, 1),
        "attempts": task.attempts,
        "verified": task.verified,
        "hash_note": task.hash_note,
        "error": task.error,
        "converted_to": task.export_mime,
    }


async def _snapshot(job_id: int) -> dict:
    """Database counters merged with the worker's in-memory live progress."""
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        payload = _job_dict(job)

    runner = get_runner()
    live = runner.progress(job_id)
    payload["running"] = runner.is_running(job_id)
    payload["active_files"] = (
        [
            {"label": label, "bytes_done": done, "total": total}
            for label, done, total in live.files.values()
        ]
        if live
        else []
    )
    payload["rate_bytes_per_second"] = round(live.rate_bytes_per_second, 1) if live else 0.0
    if live and live.rate_bytes_per_second > 0:
        remaining = max(0, payload["total_bytes"] - payload["done_bytes"])
        payload["eta_seconds"] = int(remaining / live.rate_bytes_per_second)
    else:
        payload["eta_seconds"] = None
    return payload


@router.post("")
async def create_job(body: JobCreate) -> dict:
    # Both accounts are needed whichever way the files travel.
    await require_account("google")
    await require_account("microsoft")

    if body.direction == Direction.ONEDRIVE_TO_GDRIVE:
        # Writing into Drive needs a grant the read-only connection never had,
        # and no refresh can widen it. Catch it here rather than letting every
        # file fail with a bare 403.
        google = await load_account("google")
        if google is not None and not google_can_write(google.scope):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This Google account is connected with read-only access, so "
                    "files cannot be written into Drive. Disconnect Google Drive "
                    "in step 2 and connect it again to grant write access."
                ),
            )

    dest_id = body.dest_folder_id
    dest_path = body.dest_folder_path

    if dest_id == "root":
        # "root" means different things to the two providers, so ask whichever
        # one is receiving this job.
        settings = get_settings()
        async with build_client() as client:
            _, dest = build_providers(body.direction, settings, client)
            try:
                root = await dest.get_root()
            except ProviderError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        dest_id = root.id
        dest_path = dest_path or root.name

    async with session_scope() as session:
        job = Job(
            status=JobStatus.PENDING,
            direction=body.direction,
            source_folder_id=body.source_folder_id,
            source_folder_name=body.source_folder_name,
            dest_folder_id=dest_id,
            dest_folder_path=dest_path,
            conflict_policy=body.conflict_policy,
            verify=body.verify,
            include_trashed=body.include_trashed,
        )
        session.add(job)
        await session.flush()
        job_id = job.id
        payload = _job_dict(job)

    if body.start_immediately:
        await get_runner().start(job_id)
        payload["status"] = JobStatus.PENDING

    log.info(
        "created job %d (%s): %s -> %s",
        job_id, body.direction, body.source_folder_name, dest_path,
    )
    return payload


@router.get("")
async def list_jobs(limit: int = Query(50, ge=1, le=500)) -> dict:
    async with session_scope() as session:
        jobs = list(
            (await session.execute(select(Job).order_by(Job.id.desc()).limit(limit)))
            .scalars()
            .all()
        )
    runner = get_runner()
    return {
        "jobs": [{**_job_dict(j), "running": runner.is_running(j.id)} for j in jobs]
    }


@router.get("/{job_id}")
async def get_job(job_id: int) -> dict:
    return await _snapshot(job_id)


@router.get("/{job_id}/tasks")
async def list_tasks(
    job_id: int,
    status: str | None = Query(None, description="Filter by task status"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict:
    async with session_scope() as session:
        if await session.get(Job, job_id) is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        query = select(FileTask).where(FileTask.job_id == job_id)
        if status:
            query = query.where(FileTask.status == status)
        total = (
            await session.execute(
                select(func.count()).select_from(query.subquery())
            )
        ).scalar_one()
        tasks = list(
            (
                await session.execute(
                    query.order_by(FileTask.rel_path, FileTask.id).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        counts = dict(
            (
                await session.execute(
                    select(FileTask.status, func.count())
                    .where(FileTask.job_id == job_id)
                    .group_by(FileTask.status)
                )
            ).all()
        )
    return {
        "total": total,
        "counts": counts,
        "tasks": [_task_dict(t) for t in tasks],
    }


@router.post("/{job_id}/start")
async def start_job(job_id: int) -> dict:
    runner = get_runner()
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        if runner.is_running(job_id):
            raise HTTPException(status_code=409, detail="This job is already running")
        job.status = JobStatus.PENDING
        job.error = None
        job.finished_at = None
    await runner.start(job_id)
    return await _snapshot(job_id)


@router.post("/{job_id}/pause")
async def pause_job(job_id: int) -> dict:
    paused = await get_runner().pause(job_id)
    if not paused:
        raise HTTPException(status_code=409, detail="This job is not running")
    # Give the worker a moment to settle so the response reflects reality.
    await asyncio.sleep(0.2)
    return await _snapshot(job_id)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int) -> dict:
    async with session_scope() as session:
        if await session.get(Job, job_id) is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    await get_runner().cancel(job_id)
    await asyncio.sleep(0.2)
    return await _snapshot(job_id)


@router.post("/{job_id}/retry-failed")
async def retry_failed(job_id: int) -> dict:
    """Put failed files back in the queue without re-planning the job."""
    async with session_scope() as session:
        if await session.get(Job, job_id) is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        result = await session.execute(
            update(FileTask)
            .where(FileTask.job_id == job_id, FileTask.status == TaskStatus.FAILED)
            .values(status=TaskStatus.PENDING, error=None, attempts=0)
        )
        requeued = result.rowcount or 0
    if requeued:
        await get_runner().start(job_id)
    return {"requeued": requeued, **(await _snapshot(job_id))}


@router.delete("/{job_id}")
async def delete_job(job_id: int) -> dict:
    runner = get_runner()
    if runner.is_running(job_id):
        raise HTTPException(
            status_code=409, detail="Stop the job before deleting it"
        )
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        # The relationship cascades and the foreign key is ON DELETE CASCADE,
        # so deleting the job takes its file tasks with it.
        await session.delete(job)
    return {"deleted": job_id}


@router.get("/{job_id}/events")
async def job_events(job_id: int) -> StreamingResponse:
    """Server-sent progress events until the job reaches a terminal state."""

    async def stream():
        try:
            while True:
                payload = await _snapshot(job_id)
                yield f"data: {json.dumps(payload)}\n\n"
                if payload["status"] in JobStatus.TERMINAL and not payload["running"]:
                    break
                if payload["status"] == JobStatus.PAUSED and not payload["running"]:
                    break
                await asyncio.sleep(SSE_POLL_SECONDS)
        except asyncio.CancelledError:  # pragma: no cover - client went away
            raise

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{job_id}/report")
async def job_report(job_id: int, fmt: str = Query("csv", pattern="^(csv|json)$")):
    """Everything that happened, including every rename, skip and failure."""
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        tasks = list(
            (
                await session.execute(
                    select(FileTask)
                    .where(FileTask.job_id == job_id)
                    .order_by(FileTask.rel_path, FileTask.id)
                )
            )
            .scalars()
            .all()
        )
        job_payload = _job_dict(job)

    if fmt == "json":
        return {"job": job_payload, "tasks": [_task_dict(t) for t in tasks]}

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "status", "source_name", "destination_path", "renamed",
            "rename_notes", "bytes", "converted_to", "verified",
            "hash_note", "attempts", "error",
        ]
    )
    for task in tasks:
        writer.writerow(
            [
                task.status,
                task.source_name,
                join_path(task.rel_path, task.dest_name),
                "yes" if task.dest_name != task.source_name else "",
                "; ".join(task.rename_notes or []),
                task.source_size,
                task.export_mime or "",
                {True: "yes", False: "NO - MISMATCH", None: ""}[task.verified],
                task.hash_note or "",
                task.attempts,
                task.error or "",
            ]
        )
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="cloudtransfer-job-{job_id}.csv"'
        },
    )


@router.get("/{job_id}/preflight")
async def preflight(job_id: int) -> dict:
    """Compare what the job will move against the destination's free space."""
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        needed = job.total_bytes - job.done_bytes
        direction = job.direction

    settings = get_settings()
    async with build_client() as client:
        _, dest = build_providers(direction, settings, client)
        try:
            remaining, total = await dest.get_quota()
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    fits = None if remaining is None else needed <= remaining
    return {
        "bytes_to_move": needed,
        "destination": Direction.SIDES[direction][1],
        "destination_remaining": remaining,
        "destination_total": total,
        "fits": fits,
        "warning": (
            None
            if fits is not False
            else f"The job needs {needed} bytes but the destination reports only "
                 f"{remaining} bytes free."
        ),
        "settings": {"chunk_size": settings.chunk_size},
    }

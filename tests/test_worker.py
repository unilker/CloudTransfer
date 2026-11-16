"""Job execution: planning, transferring, pausing, resuming, reporting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from cloudtransfer.db import session_scope
from cloudtransfer.engine import worker as worker_module
from cloudtransfer.errors import AuthError
from cloudtransfer.models import ConflictPolicy, FileTask, Job, JobStatus, TaskStatus

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def runner(db, drive, onedrive, monkeypatch) -> worker_module.JobRunner:
    """A runner wired to the in-memory providers instead of real HTTP."""

    async def fake_token() -> str:
        return "token"

    monkeypatch.setattr(worker_module, "GoogleDriveSource", lambda *a, **k: drive)
    monkeypatch.setattr(worker_module, "OneDriveDest", lambda *a, **k: onedrive)
    monkeypatch.setattr(
        worker_module, "TokenManager", lambda *a, **k: SimpleNamespace(get_token=fake_token)
    )
    return worker_module.JobRunner(db)


async def _finish(runner: worker_module.JobRunner, job_id: int, timeout: float = 30.0) -> None:
    task = runner._tasks.get(job_id)
    assert task is not None, "the job was never started"
    await asyncio.wait_for(asyncio.shield(task), timeout)


async def _job(settings, job_id) -> Job:
    async with session_scope(settings) as session:
        return await session.get(Job, job_id)


async def _tasks(settings, job_id) -> list[FileTask]:
    async with session_scope(settings) as session:
        return list(
            (
                await session.execute(
                    select(FileTask).where(FileTask.job_id == job_id).order_by(FileTask.id)
                )
            )
            .scalars()
            .all()
        )


# ------------------------------------------------------------------ happy path


async def test_whole_tree_arrives_intact(
    db, drive, onedrive, runner, job_factory, payload_factory
):
    photos = drive.add_folder("Photos")
    year = drive.add_folder("2026", parent=photos.id)

    expected = {
        "notes.txt": b"top level",
        "Photos/beach.jpg": payload_factory(db.chunk_size + 2000),
        "Photos/2026/ny.jpg": payload_factory(5000),
    }
    drive.add_file("notes.txt", expected["notes.txt"])
    drive.add_file("beach.jpg", expected["Photos/beach.jpg"], parent=photos.id)
    drive.add_file("ny.jpg", expected["Photos/2026/ny.jpg"], parent=year.id)

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)

    assert onedrive.tree() == expected
    job = await _job(db, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.done_files == 3
    assert job.failed_files == 0
    assert job.done_bytes == sum(len(v) for v in expected.values())
    assert job.finished_at is not None


async def test_every_file_is_verified(db, drive, onedrive, runner, job_factory, payload_factory):
    drive.add_file("small.bin", payload_factory(1000))
    drive.add_file("large.bin", payload_factory(db.chunk_size + 10))

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)

    for task in await _tasks(db, job_id):
        assert task.status == TaskStatus.DONE
        assert task.verified is True, (task.dest_name, task.hash_note)
        assert "QuickXorHash" in task.hash_note


async def test_destination_folders_are_created_once(
    db, drive, onedrive, runner, job_factory
):
    """Several files in one new folder must not each try to create it."""
    folder = drive.add_folder("Shared")
    for i in range(5):
        drive.add_file(f"file{i}.txt", f"content {i}".encode(), parent=folder.id)

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)

    assert onedrive.ensure_folder_calls == [(onedrive.root.id, "Shared")]
    assert len(onedrive.tree()) == 5


async def test_native_documents_are_converted(db, drive, onedrive, runner, job_factory):
    drive.add_native("Plan", "application/vnd.google-apps.document", {DOCX: b"PK docx"})

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)

    assert onedrive.tree() == {"Plan.docx": b"PK docx"}
    assert (await _job(db, job_id)).status == JobStatus.COMPLETED


# ------------------------------------------------------------------- failures


async def test_a_failing_file_does_not_stop_the_others(
    db, drive, onedrive, runner, job_factory, payload_factory
):
    good = payload_factory(2000)
    drive.add_file("good.bin", good)
    drive.add_file("denied.bin", payload_factory(2000))
    onedrive.fail_permanently.add("denied.bin")

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)

    assert onedrive.tree() == {"good.bin": good}
    job = await _job(db, job_id)
    assert job.status == JobStatus.COMPLETED_WITH_ERRORS
    assert job.done_files == 1
    assert job.failed_files == 1

    failed = [t for t in await _tasks(db, job_id) if t.status == TaskStatus.FAILED]
    assert len(failed) == 1
    assert "access denied" in failed[0].error


async def test_skipped_files_are_reported_not_failed(db, drive, onedrive, runner, job_factory):
    drive.add_file("fine.txt", b"ok")
    drive.add_native("Survey", "application/vnd.google-apps.form", {})

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)

    job = await _job(db, job_id)
    assert job.status == JobStatus.COMPLETED  # a skip is not an error
    assert job.skipped_files == 1
    assert job.done_files == 1


async def test_an_auth_failure_fails_the_whole_job(
    db, drive, onedrive, runner, job_factory, monkeypatch
):
    """A dead token affects every file, so stopping is right."""
    drive.add_file("a.txt", b"a")

    async def boom(item_id):
        raise AuthError("the refresh token was revoked")

    monkeypatch.setattr(drive, "get_item", boom)

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)

    job = await _job(db, job_id)
    assert job.status == JobStatus.FAILED
    assert "revoked" in job.error


async def test_retrying_a_failed_file_succeeds_once_the_cause_is_gone(
    db, drive, onedrive, runner, job_factory, payload_factory
):
    data = payload_factory(2000)
    drive.add_file("flaky.bin", data)
    onedrive.fail_permanently.add("flaky.bin")

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)
    assert (await _job(db, job_id)).failed_files == 1

    # Whatever blocked it is resolved; requeue and run again.
    onedrive.fail_permanently.clear()
    async with session_scope(db) as session:
        task = (
            await session.execute(
                select(FileTask).where(FileTask.job_id == job_id)
            )
        ).scalar_one()
        task.status = TaskStatus.PENDING
        task.error = None

    await runner.start(job_id)
    await _finish(runner, job_id)

    assert onedrive.tree() == {"flaky.bin": data}
    job = await _job(db, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.failed_files == 0


# -------------------------------------------------------------- pause/resume


async def test_pause_then_resume_completes_the_transfer(
    db, drive, onedrive, runner, job_factory, payload_factory
):
    expected = {
        f"file{i}.bin": payload_factory(db.chunk_size * 2 + 500) for i in range(4)
    }
    for name, content in expected.items():
        drive.add_file(name, content)

    onedrive.chunk_delay = 0.01  # give the pause somewhere to land

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)

    # Wait until bytes are actually moving, then stop.
    for _ in range(600):
        live = runner.progress(job_id)
        if live and live.bytes_now > 0:
            break
        await asyncio.sleep(0.01)
    assert await runner.pause(job_id) is True
    await _finish(runner, job_id)

    paused = await _job(db, job_id)
    assert paused.status == JobStatus.PAUSED
    assert onedrive.tree() != expected  # not everything got through

    remaining = [t for t in await _tasks(db, job_id) if t.status == TaskStatus.PENDING]
    assert remaining, "a paused job must leave work pending"

    # Resume and let it run to the end.
    onedrive.chunk_delay = 0.0
    await runner.start(job_id)
    await _finish(runner, job_id)

    assert onedrive.tree() == expected
    done = await _job(db, job_id)
    assert done.status == JobStatus.COMPLETED
    assert done.done_files == 4


async def test_cancel_marks_the_job_cancelled(
    db, drive, onedrive, runner, job_factory, payload_factory
):
    for i in range(4):
        drive.add_file(f"c{i}.bin", payload_factory(db.chunk_size * 2))
    onedrive.chunk_delay = 0.01

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)

    for _ in range(600):
        live = runner.progress(job_id)
        if live and live.bytes_now > 0:
            break
        await asyncio.sleep(0.01)
    await runner.cancel(job_id)
    await _finish(runner, job_id)

    assert (await _job(db, job_id)).status == JobStatus.CANCELLED


async def test_a_paused_upload_keeps_its_session_for_resume(
    db, drive, onedrive, runner, job_factory, payload_factory
):
    """The stored session URL is what makes resume continue mid-file."""
    drive.add_file("one.bin", payload_factory(db.chunk_size * 4))
    onedrive.chunk_delay = 0.02

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)

    for _ in range(600):
        live = runner.progress(job_id)
        if live and live.bytes_now > 0:
            break
        await asyncio.sleep(0.01)
    await runner.pause(job_id)
    await _finish(runner, job_id)

    task = (await _tasks(db, job_id))[0]
    assert task.status == TaskStatus.PENDING
    assert task.upload_url, "the upload session URL must survive a pause"
    assert task.bytes_done > 0


# ---------------------------------------------------------------- conflicts


async def test_skip_policy_leaves_the_destination_alone(
    db, drive, onedrive, runner, job_factory
):
    await onedrive.simple_upload(onedrive.root.id, "shared.txt", b"the original")
    drive.add_file("shared.txt", b"the replacement")

    job_id = await job_factory(
        "root", onedrive.root.id, conflict_policy=ConflictPolicy.SKIP
    )
    await runner.start(job_id)
    await _finish(runner, job_id)

    assert onedrive.tree() == {"shared.txt": b"the original"}
    job = await _job(db, job_id)
    assert job.skipped_files == 1
    assert job.done_files == 0


async def test_replace_policy_overwrites(db, drive, onedrive, runner, job_factory):
    await onedrive.simple_upload(onedrive.root.id, "shared.txt", b"the original")
    drive.add_file("shared.txt", b"the replacement")

    job_id = await job_factory(
        "root", onedrive.root.id, conflict_policy=ConflictPolicy.REPLACE
    )
    await runner.start(job_id)
    await _finish(runner, job_id)

    assert onedrive.tree() == {"shared.txt": b"the replacement"}


# ------------------------------------------------------------------- restart


async def test_stale_running_jobs_become_resumable(db, drive, onedrive, job_factory):
    """A process that dies mid-job must not leave it stuck in 'running'."""
    job_id = await job_factory("root", onedrive.root.id)
    async with session_scope(db) as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.RUNNING
        session.add(
            FileTask(
                job_id=job_id,
                status=TaskStatus.RUNNING,
                source_id="x",
                source_name="x.bin",
                dest_name="x.bin",
                upload_url="https://upload.invalid/session/1",
                bytes_done=1024,
            )
        )

    reset = await worker_module.reset_stale_jobs(db)

    assert reset == 1
    assert (await _job(db, job_id)).status == JobStatus.PAUSED
    task = (await _tasks(db, job_id))[0]
    assert task.status == TaskStatus.PENDING
    # Progress and the session survive, so the file continues where it stopped.
    assert task.upload_url == "https://upload.invalid/session/1"
    assert task.bytes_done == 1024


async def test_empty_job_completes_immediately(db, onedrive, runner, job_factory):
    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await _finish(runner, job_id)

    job = await _job(db, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.total_files == 0


# ------------------------------------------------- stopping while it counts


def _deep_tree(drive, depth: int = 12):
    """A tree whose counting takes many round trips, as a real folder does."""
    parent = "root"
    for level in range(depth):
        folder = drive.add_folder(f"level{level}", parent=parent)
        drive.add_file(f"file{level}.txt", b"x" * 10, parent=folder.id)
        parent = folder.id


async def test_pausing_while_counting_stops_promptly(
    db, drive, onedrive, runner, job_factory
):
    """Counting a large folder is many requests. Waiting for all of them before
    acknowledging a pause makes the button look broken."""
    _deep_tree(drive)
    drive.list_delay = 0.25

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await asyncio.sleep(0.6)

    assert await runner.pause(job_id) is True
    started = asyncio.get_running_loop().time()
    await _finish(runner, job_id)
    took = asyncio.get_running_loop().time() - started

    assert took < 1.5, f"pause took {took:.1f}s; the walk ignored the stop signal"
    job = await _job(db, job_id)
    # Nothing was transferred, so this is paused work, not finished work.
    assert job.status == JobStatus.PAUSED


async def test_cancelling_while_counting_stops_promptly(
    db, drive, onedrive, runner, job_factory
):
    _deep_tree(drive)
    drive.list_delay = 0.25

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await asyncio.sleep(0.6)

    await runner.cancel(job_id)
    started = asyncio.get_running_loop().time()
    await _finish(runner, job_id)
    assert asyncio.get_running_loop().time() - started < 1.5

    assert (await _job(db, job_id)).status == JobStatus.CANCELLED


async def test_files_move_while_the_tree_is_still_being_counted(
    db, drive, onedrive, runner, job_factory
):
    """The point of counting in the background.

    A 400 GB folder is thousands of round trips to walk. Waiting for the last
    of them before the first byte moves left a real transfer sitting on
    "counting files" for hours with nothing to show.
    """
    _deep_tree(drive)
    drive.list_delay = 0.25  # twelve folders, so roughly three seconds of walking

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)

    for _ in range(200):
        if onedrive.tree():
            break
        await asyncio.sleep(0.05)

    # Something has landed at the destination; the walk cannot have finished.
    assert onedrive.tree(), "nothing transferred while counting was still going"
    job = await _job(db, job_id)
    assert not job.plan_complete
    # The planner and the loop both used to write this field; whichever
    # committed last won, and a transferring job could read "pending".
    assert job.status == JobStatus.RUNNING

    await _finish(runner, job_id)
    job = await _job(db, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.plan_complete
    assert job.done_files == 12
    assert len(onedrive.tree()) == 12


async def test_a_half_counted_tree_is_never_filed_as_finished(
    db, drive, onedrive, runner, job_factory
):
    """A partial plan is kept, but must not be mistaken for the whole tree.

    What was counted is real work and worth keeping - throwing it away made a
    pause cost the entire walk. The danger is the other side of it: once every
    counted file has moved there is nothing outstanding, and a job that judged
    itself by that alone would report success having silently skipped whatever
    the walk never reached.
    """
    _deep_tree(drive)
    drive.list_delay = 0.25

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)
    await asyncio.sleep(0.6)
    await runner.pause(job_id)
    await _finish(runner, job_id)

    job = await _job(db, job_id)
    partial = await _tasks(db, job_id)
    assert partial, "the counting done so far should survive a pause"
    assert not job.plan_complete
    assert job.status == JobStatus.PAUSED
    assert len(partial) < 12, "the walk was supposed to be cut short"

    # Resuming carries on counting and ends up with every file, exactly once.
    drive.list_delay = 0.0
    await runner.start(job_id)
    await _finish(runner, job_id)

    job = await _job(db, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.plan_complete
    assert job.done_files == 12
    assert len(await _tasks(db, job_id)) == 12  # nothing counted twice
    assert len(onedrive.tree()) == 12


async def test_a_paused_job_reports_the_bytes_already_uploaded(
    db, drive, onedrive, runner, job_factory, payload_factory, settings
):
    """Reporting zero after a pause suggests nothing happened, when the
    destination is in fact holding the partial uploads for the resume."""
    drive.add_file("one.bin", payload_factory(settings.chunk_size * 4))
    onedrive.chunk_delay = 0.02

    job_id = await job_factory("root", onedrive.root.id)
    await runner.start(job_id)

    for _ in range(600):
        live = runner.progress(job_id)
        if live and live.bytes_now > settings.chunk_size:
            break
        await asyncio.sleep(0.01)
    await runner.pause(job_id)
    await _finish(runner, job_id)

    job = await _job(db, job_id)
    assert job.status == JobStatus.PAUSED
    assert job.done_files == 0          # no file finished
    assert job.done_bytes > 0           # but bytes did move
    assert job.done_bytes < job.total_bytes

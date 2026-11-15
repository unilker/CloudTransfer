"""Transfers in the other direction: OneDrive to Google Drive.

The engine is the same; what changes is which provider reads, which writes,
and therefore which rules apply. These tests exercise the differences that
actually bite: Google's 256 KiB chunk multiple instead of Microsoft's 320 KiB,
Drive's tolerance of duplicate and oddly-named files, and the fact that nothing
in OneDrive needs converting on the way out.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from cloudtransfer.db import session_scope
from cloudtransfer.engine import worker as worker_module
from cloudtransfer.engine.planner import plan_job
from cloudtransfer.engine.transfer import FileTransfer
from cloudtransfer.errors import SkipFile
from cloudtransfer.models import Direction, FileTask, Job, JobStatus, TaskStatus
from cloudtransfer.providers.gdrive import GOOGLE_CHUNK_MULTIPLE


@pytest.fixture
def relay(onedrive, drive, settings):
    """A relay reading from OneDrive and writing to Google Drive."""
    return FileTransfer(onedrive, drive, settings)


async def _move(relay, source, dest, entry, **kw):
    item = await source.get_item(entry.id)
    name, export_mime = source.plan_transfer_name(item)
    kw.setdefault("dest_name", name)
    kw.setdefault("export_mime", export_mime)
    return await relay.transfer(item, dest_parent_id=dest.root.id, **kw)


# ------------------------------------------------------------- the relay


async def test_a_file_moves_from_onedrive_to_drive(relay, onedrive, drive, payload_factory):
    data = payload_factory(settings_chunk := 1_310_720 * 2 + 4321)
    entry = onedrive.add_file("holiday.mp4", data)

    result = await _move(relay, onedrive, drive, entry)

    assert drive.read_file(drive.root.id, "holiday.mp4") == data
    assert result.bytes_moved == settings_chunk
    assert result.strategy == "stream"
    assert result.verified is True, result.hash_note


async def test_chunks_follow_googles_multiple_not_microsofts(
    relay, onedrive, drive, payload_factory, settings
):
    """Google wants 256 KiB; sending OneDrive's 320 KiB would be rejected."""
    data = payload_factory(settings.chunk_size * 2 + 1000)
    entry = onedrive.add_file("big.bin", data)

    await _move(relay, onedrive, drive, entry)

    sizes = [length for _, _, length in drive.chunk_log]
    assert len(sizes) > 1
    assert all(n % GOOGLE_CHUNK_MULTIPLE == 0 for n in sizes[:-1])
    assert drive.read_file(drive.root.id, "big.bin") == data


async def test_a_chunk_size_that_does_not_divide_is_rounded_down(onedrive, drive, settings):
    """One configured size cannot suit both providers exactly."""
    settings.chunk_size = 327_680 * 3  # a OneDrive multiple, not a Google one
    to_drive = FileTransfer(onedrive, drive, settings)
    to_onedrive = FileTransfer(drive, onedrive, settings)

    assert to_drive.chunk_size % GOOGLE_CHUNK_MULTIPLE == 0
    assert to_drive.chunk_size <= settings.chunk_size
    assert to_onedrive.chunk_size == settings.chunk_size  # already legal there


async def test_a_small_file_takes_the_single_request_path(relay, onedrive, drive):
    entry = onedrive.add_file("note.txt", b"a short note")
    result = await _move(relay, onedrive, drive, entry)

    assert drive.read_file(drive.root.id, "note.txt") == b"a short note"
    assert result.strategy == "simple"
    assert drive.chunk_log == []


async def test_an_empty_file_arrives(relay, onedrive, drive):
    entry = onedrive.add_file("empty.log", b"")
    result = await _move(relay, onedrive, drive, entry)
    assert drive.read_file(drive.root.id, "empty.log") == b""
    assert result.verified is True


async def test_a_dropped_upload_resumes_from_googles_offset(
    relay, onedrive, drive, payload_factory, settings
):
    data = payload_factory(settings.chunk_size * 3)
    entry = onedrive.add_file("resume.bin", data)
    drive.fail_chunk_at["resume.bin"] = settings.chunk_size * 2

    result = await _move(relay, onedrive, drive, entry)

    assert drive.read_file(drive.root.id, "resume.bin") == data
    assert result.resumed is True
    starts = [start for item_id, start in onedrive.reads if item_id == entry.id]
    assert starts == [0, settings.chunk_size * 2]


async def test_a_dropped_download_resumes(relay, onedrive, drive, payload_factory, settings):
    data = payload_factory(settings.chunk_size * 2)
    entry = onedrive.add_file("drop.bin", data)
    onedrive.fail_after[entry.id] = settings.chunk_size + 500

    result = await _move(relay, onedrive, drive, entry)

    assert drive.read_file(drive.root.id, "drop.bin") == data
    assert result.resumed is True


async def test_nothing_in_onedrive_needs_converting(relay, onedrive, drive):
    """Unlike Drive's native documents, every OneDrive file is real bytes."""
    entry = onedrive.add_file("Report.docx", b"PK\x03\x04 a real docx")
    item = await onedrive.get_item(entry.id)

    name, export_mime = onedrive.plan_transfer_name(item)

    assert name == "Report.docx"
    assert export_mime is None
    result = await _move(relay, onedrive, drive, entry)
    assert result.strategy == "simple"
    assert drive.read_file(drive.root.id, "Report.docx") == b"PK\x03\x04 a real docx"


async def test_a_onenote_notebook_is_skipped_with_a_reason(relay, onedrive, drive):
    entry = onedrive.add_package("Team notebook", package_type="oneNote")
    item = await onedrive.get_item(entry.id)

    with pytest.raises(SkipFile, match="package"):
        onedrive.plan_transfer_name(item)


# ----------------------------------------------------------- naming rules


async def test_names_onedrive_would_reject_survive_the_trip_to_drive(
    db, onedrive, drive, job_factory
):
    """Drive forbids none of OneDrive's reserved characters, so nothing is rewritten."""
    onedrive.add_file("quarter_ Q1.txt", b"x")
    onedrive.add_file("CON_", b"y")

    job_id = await job_factory(
        "root", drive.root.id, direction=Direction.ONEDRIVE_TO_GDRIVE
    )
    await plan_job(job_id, onedrive, db)

    async with session_scope(db) as session:
        tasks = list(
            (await session.execute(select(FileTask).where(FileTask.job_id == job_id)))
            .scalars()
            .all()
        )
    names = {t.dest_name for t in tasks}
    assert names == {"quarter_ Q1.txt", "CON_"}
    assert all(not t.rename_notes for t in tasks)


async def test_drive_keeps_duplicate_names_rather_than_numbering_them(
    db, onedrive, drive, job_factory
):
    """Renaming on a destination that allows duplicates would invent a difference."""
    folder = onedrive.add_folder("Shared")
    onedrive.add_file("photo.jpg", b"first", parent=folder.id)
    # OneDrive could not hold two of these, but a Drive destination can.
    onedrive.add_file("photo.jpg", b"second")

    job_id = await job_factory(
        "root", drive.root.id, direction=Direction.ONEDRIVE_TO_GDRIVE
    )
    await plan_job(job_id, onedrive, db)

    async with session_scope(db) as session:
        tasks = list(
            (await session.execute(select(FileTask).where(FileTask.job_id == job_id)))
            .scalars()
            .all()
        )
    assert sorted(t.dest_name for t in tasks) == ["photo.jpg", "photo.jpg"]


async def test_drives_larger_file_ceiling_applies(db, onedrive, drive, job_factory):
    """A file too big for OneDrive can still be fine going the other way."""
    entry = onedrive.add_file("huge.iso", b"")
    entry.reported_size = 400 * 1024**3  # 400 GB: over OneDrive's limit, under Drive's

    job_id = await job_factory(
        "root", drive.root.id, direction=Direction.ONEDRIVE_TO_GDRIVE
    )
    await plan_job(job_id, onedrive, db)

    async with session_scope(db) as session:
        task = (
            await session.execute(select(FileTask).where(FileTask.job_id == job_id))
        ).scalar_one()
    assert task.status == TaskStatus.PENDING
    assert task.error is None


async def test_a_file_over_drives_ceiling_is_skipped(db, onedrive, drive, job_factory):
    entry = onedrive.add_file("colossal.img", b"")
    entry.reported_size = 6 * 1024**4  # 6 TB, past Drive's 5 TB limit

    job_id = await job_factory(
        "root", drive.root.id, direction=Direction.ONEDRIVE_TO_GDRIVE
    )
    await plan_job(job_id, onedrive, db)

    async with session_scope(db) as session:
        task = (
            await session.execute(select(FileTask).where(FileTask.job_id == job_id))
        ).scalar_one()
    assert task.status == TaskStatus.SKIPPED
    assert "5 TB" in task.error


# --------------------------------------------------------------- the worker


@pytest.fixture
def runner(db, drive, onedrive, monkeypatch) -> worker_module.JobRunner:
    async def fake_token() -> str:
        return "token"

    monkeypatch.setattr(worker_module, "GoogleDriveSource", lambda *a, **k: drive)
    monkeypatch.setattr(worker_module, "GoogleDriveDest", lambda *a, **k: drive)
    monkeypatch.setattr(worker_module, "OneDriveSource", lambda *a, **k: onedrive)
    monkeypatch.setattr(worker_module, "OneDriveDest", lambda *a, **k: onedrive)
    monkeypatch.setattr(
        worker_module, "TokenManager", lambda *a, **k: SimpleNamespace(get_token=fake_token)
    )
    return worker_module.JobRunner(db)


async def _finish(runner, job_id, timeout: float = 30.0) -> None:
    task = runner._tasks.get(job_id)
    assert task is not None, "the job was never started"
    await asyncio.wait_for(asyncio.shield(task), timeout)


async def test_a_whole_tree_moves_from_onedrive_to_drive(
    db, onedrive, drive, runner, job_factory, payload_factory, settings
):
    docs = onedrive.add_folder("Documents")
    year = onedrive.add_folder("2026", parent=docs.id)

    expected = {
        "readme.txt": b"at the top",
        "Documents/contract.pdf": payload_factory(settings.chunk_size + 7000),
        "Documents/2026/notes.md": b"# notes",
    }
    onedrive.add_file("readme.txt", expected["readme.txt"])
    onedrive.add_file("contract.pdf", expected["Documents/contract.pdf"], parent=docs.id)
    onedrive.add_file("notes.md", expected["Documents/2026/notes.md"], parent=year.id)

    job_id = await job_factory(
        "root", drive.root.id, direction=Direction.ONEDRIVE_TO_GDRIVE
    )
    await runner.start(job_id)
    await _finish(runner, job_id)

    assert drive.tree() == expected
    async with session_scope(db) as session:
        job = await session.get(Job, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.done_files == 3
    assert job.direction == Direction.ONEDRIVE_TO_GDRIVE
    assert job.direction_label == "OneDrive to Google Drive"


async def test_the_worker_picks_providers_by_direction(db, onedrive, drive, runner, job_factory):
    """The same engine, with the ends swapped."""
    onedrive.add_file("from-onedrive.txt", b"one way")
    drive.add_file("from-drive.txt", b"the other way")

    reverse = await job_factory(
        "root", drive.root.id, direction=Direction.ONEDRIVE_TO_GDRIVE
    )
    await runner.start(reverse)
    await _finish(runner, reverse)

    forward = await job_factory(
        "root", onedrive.root.id, direction=Direction.GDRIVE_TO_ONEDRIVE
    )
    await runner.start(forward)
    await _finish(runner, forward)

    # Each file ended up on the far side from where it started.
    assert drive.tree()["from-onedrive.txt"] == b"one way"
    assert onedrive.tree()["from-drive.txt"] == b"the other way"


async def test_every_reverse_transfer_is_verified(
    db, onedrive, drive, runner, job_factory, payload_factory, settings
):
    onedrive.add_file("small.bin", payload_factory(5000))
    onedrive.add_file("large.bin", payload_factory(settings.chunk_size + 10))

    job_id = await job_factory(
        "root", drive.root.id, direction=Direction.ONEDRIVE_TO_GDRIVE
    )
    await runner.start(job_id)
    await _finish(runner, job_id)

    async with session_scope(db) as session:
        tasks = list(
            (await session.execute(select(FileTask).where(FileTask.job_id == job_id)))
            .scalars()
            .all()
        )
    for task in tasks:
        assert task.status == TaskStatus.DONE
        assert task.verified is True, (task.dest_name, task.hash_note)


async def test_a_package_is_skipped_by_the_worker(db, onedrive, drive, runner, job_factory):
    onedrive.add_file("fine.txt", b"ok")
    onedrive.add_package("Notebook")

    job_id = await job_factory(
        "root", drive.root.id, direction=Direction.ONEDRIVE_TO_GDRIVE
    )
    await runner.start(job_id)
    await _finish(runner, job_id)

    async with session_scope(db) as session:
        job = await session.get(Job, job_id)
    assert job.status == JobStatus.COMPLETED  # a skip is not an error
    assert job.skipped_files == 1
    assert job.done_files == 1

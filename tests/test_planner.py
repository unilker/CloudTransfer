"""Planning: walking the source tree and settling every destination name."""

from __future__ import annotations

from sqlalchemy import select

from cloudtransfer.config import ONEDRIVE_MAX_FILE_SIZE
from cloudtransfer.db import session_scope
from cloudtransfer.engine.planner import plan_job
from cloudtransfer.models import FileTask, Job, TaskStatus

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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


async def _job(settings, job_id) -> Job:
    async with session_scope(settings) as session:
        return await session.get(Job, job_id)


async def test_flat_folder_is_counted(db, drive, job_factory):
    drive.add_file("a.txt", b"a" * 10)
    drive.add_file("b.txt", b"b" * 20)
    job_id = await job_factory("root", "dest")

    count = await plan_job(job_id, drive, db)

    assert count == 2
    job = await _job(db, job_id)
    assert job.total_files == 2
    assert job.total_bytes == 30
    assert {t.dest_name for t in await _tasks(db, job_id)} == {"a.txt", "b.txt"}


async def test_nested_folders_become_relative_paths(db, drive, job_factory):
    photos = drive.add_folder("Photos")
    year = drive.add_folder("2026", parent=photos.id)
    drive.add_file("top.txt", b"x", parent="root")
    drive.add_file("beach.jpg", b"y", parent=photos.id)
    drive.add_file("ny.jpg", b"z", parent=year.id)

    job_id = await job_factory("root", "dest")
    await plan_job(job_id, drive, db)

    mapping = {t.rel_path + "/" + t.dest_name if t.rel_path else t.dest_name
               for t in await _tasks(db, job_id)}
    assert mapping == {"top.txt", "Photos/beach.jpg", "Photos/2026/ny.jpg"}


async def test_duplicate_sibling_names_are_separated(db, drive, job_factory):
    """Drive permits two files called the same thing in one folder."""
    drive.add_file("photo.jpg", b"first", parent="root")
    drive.add_file("photo.jpg", b"second", parent="root")

    job_id = await job_factory("root", "dest")
    await plan_job(job_id, drive, db)

    names = sorted(t.dest_name for t in await _tasks(db, job_id))
    assert names == ["photo (1).jpg", "photo.jpg"]
    renamed = [t for t in await _tasks(db, job_id) if t.dest_name != t.source_name]
    assert renamed and any("already used" in n for n in renamed[0].rename_notes)


async def test_illegal_characters_are_recorded_as_renames(db, drive, job_factory):
    drive.add_file("quarter: Q1/Q2.txt", b"data")
    job_id = await job_factory("root", "dest")
    await plan_job(job_id, drive, db)

    task = (await _tasks(db, job_id))[0]
    assert task.dest_name == "quarter_ Q1_Q2.txt"
    assert task.source_name == "quarter: Q1/Q2.txt"
    assert any("invalid on OneDrive" in note for note in task.rename_notes)


async def test_a_folder_and_a_file_cannot_share_a_name(db, drive, job_factory):
    """OneDrive has one namespace per folder; Drive does not."""
    drive.add_folder("report", parent="root")
    drive.add_file("report", b"contents", parent="root")

    job_id = await job_factory("root", "dest")
    await plan_job(job_id, drive, db)

    tasks = await _tasks(db, job_id)
    assert len(tasks) == 1
    # The folder claimed "report" first, so the file is moved aside.
    assert tasks[0].dest_name == "report (1)"


async def test_native_documents_get_an_export_format(db, drive, job_factory):
    drive.add_native("Notes", "application/vnd.google-apps.document", {DOCX: b"x"})
    job_id = await job_factory("root", "dest")
    await plan_job(job_id, drive, db)

    task = (await _tasks(db, job_id))[0]
    assert task.dest_name == "Notes.docx"
    assert task.export_mime == DOCX
    assert task.size_is_estimate is True
    assert task.status == TaskStatus.PENDING


async def test_unexportable_documents_are_skipped_with_a_reason(db, drive, job_factory):
    drive.add_native("Survey", "application/vnd.google-apps.form", {})
    job_id = await job_factory("root", "dest")

    count = await plan_job(job_id, drive, db)

    assert count == 0
    task = (await _tasks(db, job_id))[0]
    assert task.status == TaskStatus.SKIPPED
    assert "Google Forms" in task.error
    assert (await _job(db, job_id)).skipped_files == 1


async def test_files_the_account_cannot_download_are_skipped(db, drive, job_factory):
    entry = drive.add_file("locked.pdf", b"x" * 100)
    entry.can_download = False
    job_id = await job_factory("root", "dest")

    await plan_job(job_id, drive, db)

    task = (await _tasks(db, job_id))[0]
    assert task.status == TaskStatus.SKIPPED
    assert "not allowed to download" in task.error


async def test_files_over_onedrives_limit_are_skipped(db, drive, job_factory):
    entry = drive.add_file("huge.iso", b"")
    entry.reported_size = ONEDRIVE_MAX_FILE_SIZE + 1
    job_id = await job_factory("root", "dest")

    await plan_job(job_id, drive, db)

    task = (await _tasks(db, job_id))[0]
    assert task.status == TaskStatus.SKIPPED
    assert "250 GB" in task.error


async def test_shortcuts_are_followed(db, drive, job_factory):
    target = drive.add_file("real.txt", b"payload", parent="root")
    shortcut = drive.add_file(
        "link.txt", b"", mime="application/vnd.google-apps.shortcut", parent="root"
    )
    shortcut.shortcut_target = target.id

    job_id = await job_factory("root", "dest")
    await plan_job(job_id, drive, db)

    tasks = await _tasks(db, job_id)
    sources = {t.source_id for t in tasks}
    # Both the original and the shortcut resolve to the same underlying file.
    assert sources == {target.id}
    assert len(tasks) == 2


async def test_a_shortcut_loop_does_not_hang(db, drive, job_factory):
    """A folder shortcut pointing at an ancestor must not recurse forever."""
    outer = drive.add_folder("outer", parent="root")
    inner = drive.add_folder("inner", parent=outer.id)
    drive.add_file("leaf.txt", b"x", parent=inner.id)

    loop = drive.add_file(
        "back", b"", mime="application/vnd.google-apps.shortcut", parent=inner.id
    )
    loop.shortcut_target = outer.id

    job_id = await job_factory("root", "dest")
    count = await plan_job(job_id, drive, db)

    assert count == 1  # just leaf.txt


async def test_trashed_files_are_excluded_by_default(db, drive, job_factory):
    drive.add_file("keep.txt", b"a")
    gone = drive.add_file("gone.txt", b"b")
    gone.trashed = True

    job_id = await job_factory("root", "dest")
    assert await plan_job(job_id, drive, db) == 1

    job_id2 = await job_factory("root", "dest", include_trashed=True)
    assert await plan_job(job_id2, drive, db) == 2


async def test_replanning_keeps_finished_work(db, drive, job_factory):
    drive.add_file("a.txt", b"a" * 5)
    drive.add_file("b.txt", b"b" * 7)
    job_id = await job_factory("root", "dest")
    await plan_job(job_id, drive, db)

    # Mark one as transferred, as a completed run would.
    async with session_scope(db) as session:
        task = (
            await session.execute(
                select(FileTask).where(FileTask.job_id == job_id, FileTask.dest_name == "a.txt")
            )
        ).scalar_one()
        task.status = TaskStatus.DONE
        task.bytes_done = 5

    drive.add_file("c.txt", b"c" * 9)
    remaining = await plan_job(job_id, drive, db)

    assert remaining == 2  # b.txt and the new c.txt
    job = await _job(db, job_id)
    assert job.done_files == 1
    assert job.done_bytes == 5
    assert job.total_files == 3
    assert job.total_bytes == 21

    statuses = {t.dest_name: t.status for t in await _tasks(db, job_id)}
    assert statuses["a.txt"] == TaskStatus.DONE
    assert statuses["b.txt"] == TaskStatus.PENDING
    assert statuses["c.txt"] == TaskStatus.PENDING


async def test_planning_an_empty_folder_is_not_an_error(db, drive, job_factory):
    job_id = await job_factory("root", "dest")
    assert await plan_job(job_id, drive, db) == 0
    assert (await _job(db, job_id)).total_files == 0

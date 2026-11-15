"""End-to-end tests for the streaming relay.

Every test here runs the production engine against fakes that enforce the real
Graph upload rules, so what is being checked is that bytes arrive intact, that
chunking is legal, that interrupted transfers resume from the right offset, and
that a corrupted transfer is reported rather than passed off as fine.
"""

from __future__ import annotations

import hashlib

import pytest

from cloudtransfer.config import ONEDRIVE_CHUNK_MULTIPLE
from cloudtransfer.engine.transfer import FileTransfer, Hashers, verify_transfer
from cloudtransfer.errors import ProviderError, SkipFile
from cloudtransfer.providers.base import RemoteItem

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def relay(drive, onedrive, settings):
    seen: list[tuple[int, int]] = []

    async def progress(done: int, total: int) -> None:
        seen.append((done, total))

    transfer = FileTransfer(drive, onedrive, settings, progress=progress)
    transfer.progress_log = seen
    return transfer


async def _move(relay, drive, onedrive, entry, **kw):
    item = await drive.get_item(entry.id)
    name, export_mime = relay.source.plan_transfer_name(item)
    kw.setdefault("dest_name", name)
    kw.setdefault("export_mime", export_mime)
    return await relay.transfer(item, dest_parent_id=onedrive.root.id, **kw)


# ----------------------------------------------------------- basic integrity


async def test_empty_file(relay, drive, onedrive):
    entry = drive.add_file("empty.bin", b"")
    result = await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "empty.bin") == b""
    assert result.strategy == "empty"
    assert result.bytes_moved == 0
    assert result.verified is True


async def test_small_file_uses_a_single_request(relay, drive, onedrive, payload_factory):
    data = payload_factory(2048)
    entry = drive.add_file("small.bin", data)
    result = await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "small.bin") == data
    assert result.strategy == "simple"
    assert onedrive.chunk_log == []  # no upload session was opened
    assert result.verified is True


async def test_large_file_streams_in_chunks(relay, drive, onedrive, payload_factory, settings):
    size = settings.chunk_size * 2 + 123_456
    data = payload_factory(size)
    entry = drive.add_file("large.bin", data)

    result = await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "large.bin") == data
    assert result.strategy == "stream"
    assert result.bytes_moved == size
    assert result.verified is True, result.hash_note

    # Every chunk but the last must be a legal Graph chunk size.
    sizes = [length for _, _, length in onedrive.chunk_log]
    assert len(sizes) > 1
    assert all(n % ONEDRIVE_CHUNK_MULTIPLE == 0 for n in sizes[:-1])
    assert all(n == settings.chunk_size for n in sizes[:-1])
    assert sum(sizes) == size

    # Offsets must be contiguous from zero.
    offsets = [offset for _, offset, _ in onedrive.chunk_log]
    assert offsets[0] == 0
    assert offsets == sorted(offsets)


async def test_size_that_is_an_exact_multiple_of_the_chunk_size(
    relay, drive, onedrive, payload_factory, settings
):
    """The last chunk lands exactly on the final byte; no empty PUT may follow."""
    size = settings.chunk_size * 3
    data = payload_factory(size)
    entry = drive.add_file("exact.bin", data)

    result = await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "exact.bin") == data
    assert [length for _, _, length in onedrive.chunk_log] == [settings.chunk_size] * 3
    assert result.verified is True


async def test_progress_is_reported_monotonically(
    relay, drive, onedrive, payload_factory, settings
):
    size = settings.chunk_size + 50_000
    entry = drive.add_file("progress.bin", payload_factory(size))
    await _move(relay, drive, onedrive, entry)

    done_values = [done for done, _ in relay.progress_log]
    assert done_values == sorted(done_values)
    assert done_values[-1] == size


# ------------------------------------------------------------------- resume


async def test_resumes_from_the_offset_graph_reports(
    relay, drive, onedrive, payload_factory, settings
):
    """An upload that dies mid-file continues, it does not start over."""
    size = settings.chunk_size * 4 + 1000
    data = payload_factory(size)
    entry = drive.add_file("resume.bin", data)

    # Fail the third PUT, by which point two chunks are safely stored.
    onedrive.fail_chunk_at["resume.bin"] = settings.chunk_size * 2

    result = await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "resume.bin") == data
    assert result.resumed is True

    # The retry must restart the source read at the offset Graph reports it
    # holds - two whole chunks - rather than at zero.
    starts = [start for file_id, start in drive.reads if file_id == entry.id]
    assert len(starts) == 2
    assert starts[0] == 0
    assert starts[1] == settings.chunk_size * 2

    # Resumed transfers must not claim a verified hash they cannot compute.
    assert result.verified is None
    assert "resumed" in result.hash_note


async def test_recovers_when_the_source_connection_drops(
    relay, drive, onedrive, payload_factory, settings
):
    size = settings.chunk_size * 3
    data = payload_factory(size)
    entry = drive.add_file("drop.bin", data)

    # Drive cuts the stream partway through the second chunk.
    drive.fail_after[entry.id] = settings.chunk_size + 1000

    result = await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "drop.bin") == data
    assert result.resumed is True
    starts = [start for file_id, start in drive.reads if file_id == entry.id]
    assert starts == [0, settings.chunk_size]


async def test_starts_a_new_session_when_the_old_one_expired(
    relay, drive, onedrive, payload_factory, settings
):
    size = settings.chunk_size * 3
    data = payload_factory(size)
    entry = drive.add_file("expired.bin", data)

    onedrive.fail_chunk_at["expired.bin"] = settings.chunk_size
    onedrive.expire_session_at["expired.bin"] = settings.chunk_size

    await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "expired.bin") == data
    assert len(onedrive.sessions) == 2  # the dead one plus its replacement


async def test_resume_url_from_a_previous_process_is_reused(
    relay, drive, onedrive, payload_factory, settings
):
    """What a restart does: hand the stored session URL back to the engine."""
    size = settings.chunk_size * 3
    data = payload_factory(size)
    entry = drive.add_file("restart.bin", data)
    item = await drive.get_item(entry.id)

    session = await onedrive.create_upload_session(
        onedrive.root.id, "restart.bin", size=size, conflict="rename"
    )
    await onedrive.put_chunk(session.url, data[: settings.chunk_size], 0, size)

    result = await relay.transfer(
        item,
        dest_parent_id=onedrive.root.id,
        dest_name="restart.bin",
        export_mime=None,
        resume_url=session.url,
    )

    assert onedrive.read_file(onedrive.root.id, "restart.bin") == data
    assert result.resumed is True
    # Only the remainder was re-read from Drive.
    assert [s for i, s in drive.reads if i == entry.id] == [settings.chunk_size]


async def test_already_complete_upload_is_not_resent(
    relay, drive, onedrive, payload_factory, settings
):
    size = settings.chunk_size * 2
    data = payload_factory(size)
    entry = drive.add_file("done.bin", data)
    item = await drive.get_item(entry.id)

    session = await onedrive.create_upload_session(
        onedrive.root.id, "done.bin", size=size, conflict="rename"
    )
    await onedrive.put_chunk(session.url, data, 0, size)

    result = await relay.transfer(
        item,
        dest_parent_id=onedrive.root.id,
        dest_name="done.bin",
        export_mime=None,
        resume_url=session.url,
    )

    assert result.bytes_moved == size
    assert drive.reads == []  # nothing was read again


# ------------------------------------------------------- failure detection


async def test_mismatched_source_hash_is_reported(
    relay, drive, onedrive, payload_factory, settings
):
    """If Drive's checksum disagrees with the bytes we read, say so."""
    data = payload_factory(settings.chunk_size + 1000)
    entry = drive.add_file("suspect.bin", data)
    item = await drive.get_item(entry.id)
    item.md5 = hashlib.md5(b"something else entirely").hexdigest()

    result = await relay.transfer(
        item,
        dest_parent_id=onedrive.root.id,
        dest_name="suspect.bin",
        export_mime=None,
    )

    assert result.verified is False
    assert "MD5 mismatch" in result.hash_note


async def test_a_file_that_grows_mid_transfer_is_rejected(
    relay, drive, onedrive, payload_factory, settings
):
    """Pushing past the declared size would corrupt the upload."""
    data = payload_factory(settings.chunk_size * 2)
    entry = drive.add_file("growing.bin", data)
    item = await drive.get_item(entry.id)
    item.size = settings.chunk_size  # Drive under-reported

    with pytest.raises(ProviderError, match="more than"):
        await relay.transfer(
            item,
            dest_parent_id=onedrive.root.id,
            dest_name="growing.bin",
            export_mime=None,
        )


async def test_a_file_that_shrinks_mid_transfer_is_rejected(
    relay, drive, onedrive, payload_factory, settings
):
    data = payload_factory(settings.chunk_size + 100)
    entry = drive.add_file("shrinking.bin", data)
    item = await drive.get_item(entry.id)
    item.size = settings.chunk_size * 3  # Drive over-reported

    with pytest.raises(ProviderError, match="ended at"):
        await relay.transfer(
            item,
            dest_parent_id=onedrive.root.id,
            dest_name="shrinking.bin",
            export_mime=None,
        )


async def test_undownloadable_file_is_skipped(relay, drive, onedrive):
    entry = drive.add_file("locked.bin", b"x" * 100)
    entry.can_download = False
    item = await drive.get_item(entry.id)

    with pytest.raises(SkipFile, match="may not download"):
        await relay.transfer(
            item,
            dest_parent_id=onedrive.root.id,
            dest_name="locked.bin",
            export_mime=None,
        )


# ------------------------------------------------- Google native documents


async def test_google_doc_is_exported_to_docx(relay, drive, onedrive):
    body = b"PK\x03\x04 pretend this is a .docx" * 10
    entry = drive.add_native(
        "Meeting notes",
        "application/vnd.google-apps.document",
        {DOCX: body},
    )

    result = await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "Meeting notes.docx") == body
    assert result.strategy == "export"
    assert result.verified is True
    assert "converted from" in result.hash_note


async def test_google_sheet_keeps_an_existing_extension(relay, drive, onedrive):
    body = b"PK\x03\x04 spreadsheet"
    entry = drive.add_native(
        "Budget.xlsx",
        "application/vnd.google-apps.spreadsheet",
        {XLSX: body},
    )
    result = await _move(relay, drive, onedrive, entry)
    assert onedrive.read_file(onedrive.root.id, "Budget.xlsx") == body
    assert result.strategy == "export"


async def test_large_export_goes_through_an_upload_session(
    relay, drive, onedrive, payload_factory, settings
):
    body = payload_factory(settings.simple_upload_max + settings.chunk_size)
    entry = drive.add_native(
        "Big doc", "application/vnd.google-apps.document", {DOCX: body}
    )

    result = await _move(relay, drive, onedrive, entry)

    assert onedrive.read_file(onedrive.root.id, "Big doc.docx") == body
    assert len(onedrive.chunk_log) > 1
    assert result.verified is True


async def test_google_form_is_skipped_with_a_reason(relay, drive, onedrive):
    entry = drive.add_native("Survey", "application/vnd.google-apps.form", {})
    item = await drive.get_item(entry.id)

    with pytest.raises(SkipFile, match="Google Forms"):
        relay.source.plan_transfer_name(item)


async def test_native_file_cannot_be_streamed_directly(relay, drive, onedrive):
    """Guards against a regression that would upload a zero-byte Google Doc."""
    entry = drive.add_native(
        "Notes", "application/vnd.google-apps.document", {DOCX: b"x"}
    )
    item = await drive.get_item(entry.id)

    with pytest.raises(ProviderError, match="no byte stream"):
        async for _ in drive.open_stream(item):
            pass


# ------------------------------------------------------- conflict behaviour


async def test_rename_keeps_both_copies(relay, drive, onedrive, payload_factory):
    data_a = payload_factory(1000)
    data_b = payload_factory(1000)
    await onedrive.simple_upload(onedrive.root.id, "clash.bin", data_a)

    entry = drive.add_file("clash.bin", data_b)
    await _move(relay, drive, onedrive, entry, conflict="rename")

    tree = onedrive.tree()
    assert tree["clash.bin"] == data_a
    assert tree["clash (1).bin"] == data_b


async def test_replace_overwrites(relay, drive, onedrive, payload_factory):
    data_new = payload_factory(1000)
    await onedrive.simple_upload(onedrive.root.id, "clash.bin", b"old content")

    entry = drive.add_file("clash.bin", data_new)
    await _move(relay, drive, onedrive, entry, conflict="replace")

    tree = onedrive.tree()
    assert tree == {"clash.bin": data_new}


# -------------------------------------------------------- verify_transfer()


def test_verify_reports_no_claim_when_nothing_is_comparable():
    source = RemoteItem(id="a", name="a.bin", size=10)
    hashes = Hashers()
    hashes.update(b"0123456789")
    verdict, note = verify_transfer(source, hashes, RemoteItem(id="b", name="a.bin"))
    assert verdict is None
    assert "comparable" in note


def test_verify_detects_a_destination_size_mismatch():
    source = RemoteItem(id="a", name="a.bin", size=10, md5=hashlib.md5(b"0123456789").hexdigest())
    hashes = Hashers()
    hashes.update(b"0123456789")
    dest = RemoteItem(id="b", name="a.bin", size=9)
    verdict, note = verify_transfer(source, hashes, dest)
    assert verdict is False
    assert "size mismatch" in note


def test_verify_makes_no_claim_without_hashes():
    verdict, note = verify_transfer(RemoteItem(id="a", name="a"), None, None)
    assert verdict is None
    assert "resumed" in note

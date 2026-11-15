"""In-memory stand-ins for Google Drive and OneDrive.

The fake OneDrive enforces the constraints Microsoft Graph actually enforces -
the 320 KiB chunk multiple, offsets that must line up, a total size that must
match - so a test passing here means the engine is obeying the real rules, not
just the ones it happens to remember.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import AsyncIterator

import pytest

from cloudtransfer.config import ONEDRIVE_CHUNK_MULTIPLE, Settings
from cloudtransfer.errors import NotFoundError, ProviderError, RetryableError, SkipFile
from cloudtransfer.providers.base import RemoteItem, UploadSession
from cloudtransfer.providers.gdrive import FOLDER_MIME, GoogleDriveSource
from cloudtransfer.quickxor import quickxorhash


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        secret_key="test-secret",
        chunk_size=ONEDRIVE_CHUNK_MULTIPLE * 2,  # 640 KiB, small enough to be fast
        # Force the streaming path for modest payloads; in production this is
        # 4 MiB, but tests should not need multi-megabyte fixtures to reach it.
        simple_upload_max=ONEDRIVE_CHUNK_MULTIPLE // 2,
        max_parallel_files=2,
        max_retries=4,
        verify_hashes=True,
    )


# --------------------------------------------------------------- fake Drive


_drive_ids = itertools.count(1)


class FakeDriveFile:
    def __init__(self, name, content=b"", mime="application/octet-stream", parent="root"):
        self.id = f"d{next(_drive_ids)}"
        self.name = name
        self.content = content
        self.mime = mime
        self.parent = parent
        self.can_download = True
        self.trashed = False
        self.export_payloads: dict[str, bytes] = {}
        self.shortcut_target: str | None = None
        # Override the size Drive reports, to stand in for a file too big for
        # OneDrive without allocating it.
        self.reported_size: int | None = None


class FakeDrive:
    """Minimal Drive: folders, files, Range reads, exports, fault injection."""

    def __init__(self) -> None:
        self.items: dict[str, FakeDriveFile] = {}
        self.reads: list[tuple[str, int]] = []
        # file id -> drop the stream after this many bytes, once
        self.fail_after: dict[str, int] = {}
        self.range_supported = True

    # -- construction helpers
    def add_folder(self, name: str, parent: str = "root") -> FakeDriveFile:
        folder = FakeDriveFile(name, mime=FOLDER_MIME, parent=parent)
        self.items[folder.id] = folder
        return folder

    def add_file(self, name, content=b"", mime="application/octet-stream", parent="root"):
        entry = FakeDriveFile(name, content=content, mime=mime, parent=parent)
        self.items[entry.id] = entry
        return entry

    def add_native(self, name, mime, exports: dict[str, bytes], parent="root"):
        entry = FakeDriveFile(name, mime=mime, parent=parent)
        entry.export_payloads = exports
        self.items[entry.id] = entry
        return entry

    # -- provider surface
    def _to_item(self, entry: FakeDriveFile) -> RemoteItem:
        import hashlib

        is_native = entry.mime.startswith("application/vnd.google-apps.")
        if is_native:
            size = None
        elif entry.reported_size is not None:
            size = entry.reported_size
        else:
            size = len(entry.content)
        return RemoteItem(
            id=entry.id,
            name=entry.name,
            mime_type=entry.mime,
            size=size,
            is_folder=entry.mime == FOLDER_MIME,
            md5=None if is_native else hashlib.md5(entry.content).hexdigest(),
            parent_id=entry.parent,
            can_download=entry.can_download,
            trashed=entry.trashed,
            shortcut_target_id=entry.shortcut_target,
        )

    async def get_item(self, item_id: str) -> RemoteItem:
        entry = self.items.get(item_id)
        if entry is None:
            raise NotFoundError(f"no such Drive item {item_id}", status=404)
        return self._to_item(entry)

    async def list_children(self, folder_id: str, *, include_trashed: bool = False):
        return [
            self._to_item(e)
            for e in self.items.values()
            if e.parent == folder_id and (include_trashed or not e.trashed)
        ]

    async def resolve_shortcut(self, item: RemoteItem) -> RemoteItem:
        if not item.is_shortcut:
            return item
        target = await self.get_item(item.shortcut_target_id)
        target.name = item.name
        return target

    # Reuse the production naming/export decision so tests cover the real one.
    plan_transfer_name = GoogleDriveSource.plan_transfer_name

    async def open_stream(self, item: RemoteItem, *, start: int = 0) -> AsyncIterator[bytes]:
        entry = self.items[item.id]
        # The real adapter refuses these two cases; the fake must too, or the
        # engine could regress into sending a zero-byte Google Doc.
        if entry.mime.startswith("application/vnd.google-apps."):
            raise ProviderError(f"{item.name!r} is native and has no byte stream")
        if not entry.can_download:
            raise SkipFile(f"this account may not download {item.name!r}")
        self.reads.append((item.id, start))

        data = entry.content
        if self.range_supported:
            data = data[start:]
            emitted_base = start
        else:
            # Server ignored the Range header; the adapter must cope.
            emitted_base = 0

        limit = self.fail_after.pop(item.id, None)
        emitted = 0
        step = 64 * 1024
        for offset in range(0, len(data), step):
            piece = data[offset : offset + step]
            if limit is not None and emitted_base + emitted + len(piece) > limit:
                keep = max(0, limit - (emitted_base + emitted))
                if keep:
                    yield piece[:keep]
                raise RetryableError("simulated connection reset mid-download", status=503)
            emitted += len(piece)
            yield piece

    async def export_to_memory(self, item: RemoteItem, export_mime: str) -> bytes:
        entry = self.items[item.id]
        if export_mime not in entry.export_payloads:
            raise ProviderError(f"cannot export {item.name} as {export_mime}", status=400)
        return entry.export_payloads[export_mime]

    async def close(self) -> None:
        return None


# ------------------------------------------------------------ fake OneDrive


_one_ids = itertools.count(1)


class FakeOneItem:
    def __init__(self, name, parent, is_folder=False, content=b""):
        self.id = f"o{next(_one_ids)}"
        self.name = name
        self.parent = parent
        self.is_folder = is_folder
        self.content = content


class FakeUpload:
    def __init__(self, parent, name, total, conflict):
        self.parent = parent
        self.name = name
        self.total = total
        self.conflict = conflict
        self.buffer = bytearray()
        self.committed = False
        self.alive = True


class FakeOneDrive:
    """Minimal OneDrive that enforces Graph's upload-session rules."""

    def __init__(self) -> None:
        self.root = FakeOneItem("OneDrive", parent=None, is_folder=True)
        self.items: dict[str, FakeOneItem] = {self.root.id: self.root}
        self.sessions: dict[str, FakeUpload] = {}
        self._counter = 0
        self.chunk_log: list[tuple[str, int, int]] = []
        # name -> fail the PUT that would cross this byte offset, once
        self.fail_chunk_at: dict[str, int] = {}
        self.expire_session_at: dict[str, int] = {}
        self.quota_remaining: int | None = 10 * 1024**4
        # Names that always fail, standing in for a permission or policy error.
        self.fail_permanently: set[str] = set()
        # Artificial per-chunk delay, so pause/cancel tests have a window to
        # land in rather than racing an in-memory transfer to completion.
        self.chunk_delay: float = 0.0
        self.ensure_folder_calls: list[tuple[str, str]] = []

    # -- helpers
    def _children(self, parent_id):
        return [i for i in self.items.values() if i.parent == parent_id]

    def _to_item(self, entry: FakeOneItem) -> RemoteItem:
        return RemoteItem(
            id=entry.id,
            name=entry.name,
            size=None if entry.is_folder else len(entry.content),
            is_folder=entry.is_folder,
            quick_xor=None if entry.is_folder else quickxorhash(entry.content),
            parent_id=entry.parent,
        )

    def _free_name(self, parent_id, name, conflict):
        existing = {i.name.casefold(): i for i in self._children(parent_id)}
        if name.casefold() not in existing:
            return name, None
        if conflict == "fail":
            raise ProviderError(f"{name} already exists", status=409)
        if conflict == "replace":
            return name, existing[name.casefold()]
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        else:
            ext = dot + ext
        n = 1
        while f"{stem} ({n}){ext}".casefold() in existing:
            n += 1
        return f"{stem} ({n}){ext}", None

    def _store(self, parent_id, name, content, conflict):
        final, replacing = self._free_name(parent_id, name, conflict)
        if replacing is not None:
            replacing.content = content
            return replacing
        entry = FakeOneItem(final, parent_id, content=content)
        self.items[entry.id] = entry
        return entry

    # -- provider surface
    async def get_root(self) -> RemoteItem:
        return self._to_item(self.root)

    async def get_item(self, item_id: str) -> RemoteItem:
        entry = self.items.get(item_id)
        if entry is None:
            raise NotFoundError(f"no such OneDrive item {item_id}", status=404)
        return self._to_item(entry)

    async def list_children(self, folder_id: str):
        return [self._to_item(i) for i in self._children(folder_id)]

    async def find_child(self, parent_id: str, name: str) -> RemoteItem | None:
        for entry in self._children(parent_id):
            if entry.name.casefold() == name.casefold():
                return self._to_item(entry)
        return None

    async def ensure_folder(self, parent_id: str, name: str) -> RemoteItem:
        self.ensure_folder_calls.append((parent_id, name))
        found = await self.find_child(parent_id, name)
        if found is not None and found.is_folder:
            return found
        entry = FakeOneItem(name, parent_id, is_folder=True)
        self.items[entry.id] = entry
        return self._to_item(entry)

    async def create_upload_session(self, parent_id, name, *, size, conflict="rename"):
        if name in self.fail_permanently:
            raise ProviderError(f"access denied for {name}", status=403)
        if self.quota_remaining is not None and size > self.quota_remaining:
            raise ProviderError("insufficient storage", status=507)
        self._counter += 1
        url = f"https://fake-upload.invalid/session/{self._counter}"
        self.sessions[url] = FakeUpload(parent_id, name, size, conflict)
        return UploadSession(url=url)

    async def session_next_offset(self, session_url: str) -> int | None:
        session = self.sessions.get(session_url)
        if session is None or not session.alive or session.committed:
            return None
        return len(session.buffer)

    async def put_chunk(self, session_url, data, offset, total):
        session = self.sessions.get(session_url)
        if session is None or not session.alive:
            raise NotFoundError("upload session is gone", status=404)
        if session.committed:
            raise ProviderError("session already committed", status=409)
        if total != session.total:
            raise ProviderError(
                f"Content-Range total {total} does not match the declared "
                f"{session.total}", status=400
            )
        if offset != len(session.buffer):
            raise ProviderError(
                f"expected bytes at {len(session.buffer)}, got {offset}", status=416
            )

        end = offset + len(data) - 1
        is_last = end == total - 1
        if not is_last and len(data) % ONEDRIVE_CHUNK_MULTIPLE != 0:
            raise ProviderError(
                f"chunk of {len(data)} bytes is not a multiple of 320 KiB", status=400
            )

        trip = self.fail_chunk_at.get(session.name)
        if trip is not None and end >= trip:
            del self.fail_chunk_at[session.name]
            if session.name in self.expire_session_at:
                session.alive = False
                del self.expire_session_at[session.name]
            raise RetryableError("simulated upload failure", status=503)

        if self.chunk_delay:
            await asyncio.sleep(self.chunk_delay)

        session.buffer.extend(data)
        self.chunk_log.append((session.name, offset, len(data)))

        if len(session.buffer) >= session.total:
            session.committed = True
            entry = self._store(
                session.parent, session.name, bytes(session.buffer), session.conflict
            )
            return self._to_item(entry)
        return None

    async def cancel_session(self, session_url: str) -> None:
        session = self.sessions.get(session_url)
        if session is not None:
            session.alive = False

    async def simple_upload(self, parent_id, name, data, *, conflict="rename"):
        if name in self.fail_permanently:
            raise ProviderError(f"access denied for {name}", status=403)
        return self._to_item(self._store(parent_id, name, data, conflict))

    async def delete_item(self, item_id: str) -> None:
        self.items.pop(item_id, None)

    async def get_quota(self):
        return self.quota_remaining, 10 * 1024**4

    async def close(self) -> None:
        return None

    # -- assertions helpers
    def read_file(self, parent_id, name) -> bytes:
        for entry in self._children(parent_id):
            if entry.name == name:
                return entry.content
        raise AssertionError(f"{name!r} is not in the destination")

    def tree(self, parent_id=None, prefix="") -> dict[str, bytes]:
        parent_id = parent_id or self.root.id
        out: dict[str, bytes] = {}
        for entry in self._children(parent_id):
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.is_folder:
                out.update(self.tree(entry.id, path))
            else:
                out[path] = entry.content
        return out


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
def onedrive() -> FakeOneDrive:
    return FakeOneDrive()


@pytest.fixture
def payload_factory():
    def make(size: int) -> bytes:
        return os.urandom(size)

    return make


# ------------------------------------------------------------------- database


@pytest.fixture
async def db(settings):
    """A fresh SQLite file per test.

    ``db.py`` caches its engine in module globals, so it has to be disposed
    around every test or the second one would quietly reuse the first's file.
    """
    from cloudtransfer.db import dispose_db, init_db

    await dispose_db()
    await init_db(settings)
    try:
        yield settings
    finally:
        await dispose_db()


@pytest.fixture
async def job_factory(db):
    from cloudtransfer.db import session_scope
    from cloudtransfer.models import Job, JobStatus

    async def make(source_folder_id: str, dest_folder_id: str, **kw) -> int:
        async with session_scope(db) as session:
            job = Job(
                status=JobStatus.PENDING,
                source_folder_id=source_folder_id,
                source_folder_name=kw.pop("source_folder_name", "source"),
                dest_folder_id=dest_folder_id,
                dest_folder_path=kw.pop("dest_folder_path", "OneDrive"),
                **kw,
            )
            session.add(job)
            await session.flush()
            return job.id

    return make

"""In-memory stand-ins for Google Drive and OneDrive.

One class plays both roles, exactly as the real adapters do: each provider can
be the source of a transfer or its destination, and a test picks which. That is
what makes both directions testable against the production engine rather than
against a simplified imitation of it.

The fakes enforce the constraints the real services enforce - the per-provider
chunk multiple, offsets that must line up, a declared total that must match,
OneDrive's unique names against Drive's duplicates - so a passing test means
the engine obeys the real rules.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
from collections.abc import AsyncIterator

import pytest

from cloudtransfer.config import ONEDRIVE_CHUNK_MULTIPLE, Settings
from cloudtransfer.errors import NotFoundError, ProviderError, RetryableError, SkipFile
from cloudtransfer.providers.base import RemoteItem, UploadSession
from cloudtransfer.providers.gdrive import FOLDER_MIME, GOOGLE_CHUNK_MULTIPLE
from cloudtransfer.providers.gdrive import GoogleDriveSource as _RealDriveSource
from cloudtransfer.quickxor import quickxorhash

_ids = itertools.count(1)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        secret_key="test-secret",
        # A multiple of both providers' chunk rules (320 KiB and 256 KiB), so
        # neither destination has to round it down in tests.
        chunk_size=1_310_720,
        # Force the streaming path for modest payloads; in production this is
        # 4 MiB, but tests should not need multi-megabyte fixtures to reach it.
        simple_upload_max=100_000,
        max_parallel_files=2,
        max_retries=4,
        verify_hashes=True,
    )


# --------------------------------------------------------------------- nodes


class FakeNode:
    def __init__(self, name, parent, *, is_folder=False, content=b"", mime=""):
        self.id = f"n{next(_ids)}"
        self.name = name
        self.parent = parent
        self.is_folder = is_folder
        self.content = content
        self.mime = mime or (FOLDER_MIME if is_folder else "application/octet-stream")
        self.can_download = True
        self.trashed = False
        self.export_payloads: dict[str, bytes] = {}
        self.shortcut_target: str | None = None
        self.package_type: str | None = None
        # Override the size the provider reports, to stand in for a file too
        # large for the destination without allocating it.
        self.reported_size: int | None = None


class FakeUpload:
    def __init__(self, parent, name, total, conflict):
        self.parent = parent
        self.name = name
        self.total = total
        self.conflict = conflict
        self.buffer = bytearray()
        self.committed = False
        self.alive = True


# --------------------------------------------------------------------- cloud


class FakeCloud:
    """A provider that can act as either end of a transfer.

    ``provider`` is ``"gdrive"`` or ``"onedrive"``, which decides the chunk
    multiple it enforces, whether sibling names must be unique, and whether it
    has native documents that need exporting.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.is_drive = provider == "gdrive"
        self.chunk_multiple = (
            GOOGLE_CHUNK_MULTIPLE if self.is_drive else ONEDRIVE_CHUNK_MULTIPLE
        )
        # OneDrive treats one name per folder as unique; Drive does not.
        self.unique_names = not self.is_drive

        self.root = FakeNode("My Drive" if self.is_drive else "OneDrive", None, is_folder=True)
        self.items: dict[str, FakeNode] = {self.root.id: self.root}
        self.sessions: dict[str, FakeUpload] = {}
        self._session_counter = 0

        # -- observation
        self.reads: list[tuple[str, int]] = []
        self.chunk_log: list[tuple[str, int, int]] = []
        self.ensure_folder_calls: list[tuple[str, str]] = []

        # -- fault injection
        self.fail_after: dict[str, int] = {}
        self.fail_chunk_at: dict[str, int] = {}
        self.expire_session_at: dict[str, int] = {}
        self.fail_permanently: set[str] = set()
        self.range_supported = True
        self.chunk_delay = 0.0
        self.quota_remaining: int | None = 10 * 1024**4

    # -- construction ------------------------------------------------------

    def _resolve(self, parent_id: str | None) -> str:
        if parent_id in (None, "", "root"):
            return self.root.id
        return parent_id

    def add_folder(self, name: str, parent: str = "root") -> FakeNode:
        node = FakeNode(name, self._resolve(parent), is_folder=True)
        self.items[node.id] = node
        return node

    def add_file(self, name, content=b"", mime="application/octet-stream", parent="root"):
        node = FakeNode(name, self._resolve(parent), content=content, mime=mime)
        self.items[node.id] = node
        return node

    def add_native(self, name, mime, exports: dict[str, bytes], parent="root"):
        """A Google-native document, which has no bytes of its own."""
        node = FakeNode(name, self._resolve(parent), mime=mime)
        node.export_payloads = exports
        self.items[node.id] = node
        return node

    def add_package(self, name, package_type="oneNote", parent="root"):
        """A OneDrive package, such as a OneNote notebook."""
        node = FakeNode(name, self._resolve(parent))
        node.package_type = package_type
        self.items[node.id] = node
        return node

    # -- mapping -----------------------------------------------------------

    def _children(self, parent_id: str) -> list[FakeNode]:
        parent_id = self._resolve(parent_id)
        return [n for n in self.items.values() if n.parent == parent_id]

    def _to_item(self, node: FakeNode) -> RemoteItem:
        native = node.mime.startswith("application/vnd.google-apps.") and not node.is_folder
        if node.is_folder or native:
            size = None
        elif node.reported_size is not None:
            size = node.reported_size
        else:
            size = len(node.content)

        extra = {}
        if node.package_type:
            extra["package_type"] = node.package_type

        return RemoteItem(
            id=node.id,
            name=node.name,
            mime_type=node.mime,
            size=size,
            is_folder=node.is_folder,
            md5=None if (node.is_folder or native) else hashlib.md5(node.content).hexdigest(),
            quick_xor=None if (node.is_folder or native) else quickxorhash(node.content),
            parent_id=node.parent,
            can_download=node.can_download,
            trashed=node.trashed,
            shortcut_target_id=node.shortcut_target,
            extra=extra,
        )

    # ======================================================= source interface

    async def get_root(self) -> RemoteItem:
        return self._to_item(self.root)

    async def get_item(self, item_id: str) -> RemoteItem:
        node = self.items.get(self._resolve(item_id))
        if node is None:
            raise NotFoundError(f"no such item {item_id}", status=404)
        return self._to_item(node)

    async def list_children(
        self, folder_id: str, *, include_trashed: bool = False
    ) -> list[RemoteItem]:
        return [
            self._to_item(n)
            for n in self._children(folder_id)
            if include_trashed or not n.trashed
        ]

    async def resolve_shortcut(self, item: RemoteItem) -> RemoteItem:
        if not item.is_shortcut:
            return item
        target = await self.get_item(item.shortcut_target_id)
        target.name = item.name
        return target

    def plan_transfer_name(self, item: RemoteItem) -> tuple[str, str | None]:
        """Reuse the production decision for Drive; OneDrive converts nothing."""
        if self.is_drive:
            return _RealDriveSource.plan_transfer_name(self, item)
        if item.extra.get("package_type"):
            raise SkipFile(
                f"items stored as a '{item.extra['package_type']}' package "
                f"cannot be downloaded"
            )
        return item.name, None

    async def open_stream(self, item: RemoteItem, *, start: int = 0) -> AsyncIterator[bytes]:
        node = self.items[item.id]
        # The real adapters refuse these; the fakes must too, or the engine
        # could regress into sending a zero-byte document.
        if node.mime.startswith("application/vnd.google-apps.") and not node.is_folder:
            raise ProviderError(f"{item.name!r} is native and has no byte stream")
        if node.package_type:
            raise SkipFile(f"{item.name!r} is a package and cannot be downloaded")
        if not node.can_download:
            raise SkipFile(f"this account may not download {item.name!r}")

        self.reads.append((item.id, start))

        data = node.content
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
        node = self.items[item.id]
        if export_mime not in node.export_payloads:
            raise ProviderError(f"cannot export {item.name} as {export_mime}", status=400)
        return node.export_payloads[export_mime]

    async def list_shared_drives(self) -> list[RemoteItem]:
        return []

    # ================================================== destination interface

    def _free_name(self, parent_id, name, conflict):
        """Apply the destination's own conflict behaviour."""
        siblings = self._children(parent_id)
        if not self.unique_names:
            # Drive keeps duplicates happily; only "replace" looks for a match.
            if conflict == "replace":
                for node in siblings:
                    if node.name == name and not node.is_folder:
                        return name, node
            return name, None

        existing = {n.name.casefold(): n for n in siblings}
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
        parent_id = self._resolve(parent_id)
        final, replacing = self._free_name(parent_id, name, conflict)
        if replacing is not None:
            replacing.content = content
            return replacing
        node = FakeNode(final, parent_id, content=content)
        self.items[node.id] = node
        return node

    async def find_child(self, parent_id: str, name: str) -> RemoteItem | None:
        for node in self._children(parent_id):
            matches = (
                node.name.casefold() == name.casefold()
                if self.unique_names
                else node.name == name
            )
            if matches:
                return self._to_item(node)
        return None

    async def ensure_folder(self, parent_id: str, name: str) -> RemoteItem:
        parent_id = self._resolve(parent_id)
        self.ensure_folder_calls.append((parent_id, name))
        found = await self.find_child(parent_id, name)
        if found is not None and found.is_folder:
            return found
        node = FakeNode(name, parent_id, is_folder=True)
        self.items[node.id] = node
        return self._to_item(node)

    async def create_upload_session(self, parent_id, name, *, size, conflict="rename"):
        if name in self.fail_permanently:
            raise ProviderError(f"access denied for {name}", status=403)
        if self.quota_remaining is not None and size > self.quota_remaining:
            raise ProviderError("insufficient storage", status=507)
        self._session_counter += 1
        url = f"https://fake-upload.invalid/{self.provider}/{self._session_counter}"
        self.sessions[url] = FakeUpload(self._resolve(parent_id), name, size, conflict)
        return UploadSession(url=url)

    async def session_next_offset(self, session_url: str, total: int = 0) -> int | None:
        session = self.sessions.get(session_url)
        if session is None or not session.alive or session.committed:
            return None
        if total and total != session.total:
            raise ProviderError(
                f"status query states {total} bytes, session declared {session.total}",
                status=400,
            )
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
        if not is_last and len(data) % self.chunk_multiple != 0:
            raise ProviderError(
                f"chunk of {len(data)} bytes is not a multiple of "
                f"{self.chunk_multiple}", status=400
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
            node = self._store(
                session.parent, session.name, bytes(session.buffer), session.conflict
            )
            return self._to_item(node)
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

    # -- assertions --------------------------------------------------------

    def read_file(self, parent_id, name) -> bytes:
        for node in self._children(parent_id):
            if node.name == name:
                return node.content
        raise AssertionError(f"{name!r} is not in {self.provider}")

    def tree(self, parent_id=None, prefix="") -> dict[str, bytes]:
        parent_id = self._resolve(parent_id)
        out: dict[str, bytes] = {}
        for node in self._children(parent_id):
            path = f"{prefix}/{node.name}" if prefix else node.name
            if node.is_folder:
                out.update(self.tree(node.id, path))
            else:
                out[path] = node.content
        return out


@pytest.fixture
def drive() -> FakeCloud:
    return FakeCloud("gdrive")


@pytest.fixture
def onedrive() -> FakeCloud:
    return FakeCloud("onedrive")


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
    from cloudtransfer.models import Direction, Job, JobStatus

    async def make(source_folder_id: str, dest_folder_id: str, **kw) -> int:
        async with session_scope(db) as session:
            job = Job(
                status=JobStatus.PENDING,
                direction=kw.pop("direction", Direction.GDRIVE_TO_ONEDRIVE),
                source_folder_id=source_folder_id,
                source_folder_name=kw.pop("source_folder_name", "source"),
                dest_folder_id=dest_folder_id,
                dest_folder_path=kw.pop("dest_folder_path", "destination"),
                **kw,
            )
            session.add(job)
            await session.flush()
            return job.id

    return make

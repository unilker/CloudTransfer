"""The streaming relay: Google Drive bytes into OneDrive, never touching disk.

The shape of one file transfer:

    Drive  --HTTP GET (Range)-->  [RAM buffer, one chunk]  --HTTP PUT-->  OneDrive

Only one chunk is held at a time, so a 200 GB file costs the same memory as a
200 MB one. Nothing is ever written to a filesystem; there is no temporary
directory to clean up and no partial file to leak.

Two rules keep resume correct:

* **Graph is the source of truth for progress.** After any failure the relay
  asks the upload session how many bytes it actually holds and restarts the
  Drive read from exactly there with a ``Range`` header. Its own byte counter
  is never trusted across a retry.
* **A hash is only claimed when the whole file passed through in one go.** If a
  transfer resumes from a non-zero offset the running hashes cover only part
  of the content, so verification is reported as skipped rather than guessed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..config import Settings, get_settings
from ..errors import ProviderError, SkipFile
from ..providers.base import DestProvider, RemoteItem, SourceProvider
from ..providers.gdrive import is_native
from ..quickxor import QuickXorHash

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Awaitable[None]]  # (bytes_done, total)
# Called as soon as an upload session exists, so the caller can persist the URL
# and resume this exact upload after a restart.
SessionCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class Hashers:
    """Hashes computed on the fly, as the bytes go past.

    Computing them during the relay is the whole point: it costs one pass over
    data we are already holding, and it is the only way to verify a transfer
    without reading either side back afterwards.
    """

    md5: hashlib._Hash = field(default_factory=hashlib.md5)
    sha256: hashlib._Hash = field(default_factory=hashlib.sha256)
    sha1: hashlib._Hash = field(default_factory=hashlib.sha1)
    quick_xor: QuickXorHash = field(default_factory=QuickXorHash)
    size: int = 0

    def update(self, data: bytes) -> None:
        self.md5.update(data)
        self.sha256.update(data)
        self.sha1.update(data)
        self.quick_xor.update(data)
        self.size += len(data)

    @property
    def md5_hex(self) -> str:
        return self.md5.hexdigest()

    @property
    def sha256_hex(self) -> str:
        return self.sha256.hexdigest()

    @property
    def quick_xor_b64(self) -> str:
        return self.quick_xor.b64digest()

    @property
    def sha1_b64(self) -> str:
        """Graph reports sha1Hash as hex, uppercase, on personal accounts."""
        return self.sha1.hexdigest().upper()


@dataclass(slots=True)
class TransferResult:
    item: RemoteItem | None
    bytes_moved: int
    verified: bool | None
    hash_note: str
    resumed: bool
    strategy: str


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def verify_transfer(
    source: RemoteItem,
    hashes: Hashers | None,
    dest: RemoteItem | None,
) -> tuple[bool | None, str]:
    """Compare what we read, what Drive claims, and what OneDrive stored.

    Returns ``(verdict, explanation)`` where the verdict is ``True`` (matched),
    ``False`` (mismatch - the file is suspect) or ``None`` (nothing comparable
    was available, so no claim is made).
    """
    if hashes is None:
        return None, "hashes not computed (transfer resumed mid-file)"

    checks: list[str] = []

    # 1. Did we read from Drive what Drive says is there?
    if source.md5:
        if _norm(source.md5) != _norm(hashes.md5_hex):
            return False, (
                f"source MD5 mismatch: Drive reports {source.md5}, "
                f"the bytes we read hash to {hashes.md5_hex}"
            )
        checks.append("source MD5")
    if source.sha256 and _norm(source.sha256) != _norm(hashes.sha256_hex):
        return False, (
            f"source SHA-256 mismatch: Drive reports {source.sha256}, "
            f"the bytes we read hash to {hashes.sha256_hex}"
        )
    if source.sha256:
        checks.append("source SHA-256")

    # 2. Did OneDrive store exactly those bytes?
    if dest is not None:
        if dest.size is not None and dest.size != hashes.size:
            return False, (
                f"destination size mismatch: sent {hashes.size} bytes, "
                f"OneDrive reports {dest.size}"
            )
        if dest.quick_xor:
            if dest.quick_xor.strip() != hashes.quick_xor_b64:
                return False, (
                    f"destination QuickXorHash mismatch: OneDrive reports "
                    f"{dest.quick_xor}, we computed {hashes.quick_xor_b64}"
                )
            checks.append("destination QuickXorHash")
        if dest.sha256:
            dest_sha = dest.sha256.strip()
            ours = hashes.sha256_hex
            # Graph has reported this as hex and, historically, as base64.
            ours_b64 = base64.b64encode(bytes.fromhex(ours)).decode("ascii")
            if _norm(dest_sha) not in (_norm(ours), _norm(ours_b64)):
                return False, (
                    f"destination SHA-256 mismatch: OneDrive reports {dest_sha}, "
                    f"we computed {ours}"
                )
            checks.append("destination SHA-256")

    if not checks:
        return None, "neither provider exposed a comparable hash for this file"
    return True, "verified by " + ", ".join(checks)


class FileTransfer:
    """Moves one file at a time between a source and a destination."""

    def __init__(
        self,
        source: SourceProvider,
        dest: DestProvider,
        settings: Settings | None = None,
        progress: ProgressCallback | None = None,
        on_session: SessionCallback | None = None,
    ) -> None:
        self.source = source
        self.dest = dest
        self.settings = settings or get_settings()
        self._progress = progress
        self._on_session = on_session

    async def _report(self, done: int, total: int) -> None:
        if self._progress is not None:
            await self._progress(done, total)

    async def _announce_session(self, url: str) -> None:
        if self._on_session is not None:
            await self._on_session(url)

    @property
    def chunk_size(self) -> int:
        """The configured chunk size, rounded down to what this destination takes.

        OneDrive demands multiples of 320 KiB and Google of 256 KiB, so one
        configured value cannot satisfy both exactly. Rounding down keeps the
        request legal and never exceeds what the operator asked for.
        """
        multiple = getattr(self.dest, "chunk_multiple", 0) or 1
        rounded = (self.settings.chunk_size // multiple) * multiple
        return max(rounded, multiple)

    # -- entry point --------------------------------------------------------

    async def transfer(
        self,
        item: RemoteItem,
        *,
        dest_parent_id: str,
        dest_name: str,
        export_mime: str | None,
        conflict: str = "rename",
        resume_url: str | None = None,
        verify: bool = True,
    ) -> TransferResult:
        """Move one file, picking the cheapest correct strategy."""
        if export_mime or is_native(item.mime_type):
            return await self._transfer_exported(
                item, dest_parent_id, dest_name, export_mime or "", conflict, verify
            )

        size = item.size or 0
        if size == 0:
            return await self._transfer_empty(item, dest_parent_id, dest_name, conflict)
        if size <= self.settings.simple_upload_max:
            return await self._transfer_small(item, dest_parent_id, dest_name, conflict, verify)
        return await self._transfer_streaming(
            item, dest_parent_id, dest_name, conflict, resume_url, verify
        )

    # -- strategies ---------------------------------------------------------

    async def _transfer_empty(
        self, item: RemoteItem, parent: str, name: str, conflict: str
    ) -> TransferResult:
        """Graph cannot open an upload session for zero bytes."""
        dest_item = await self.dest.simple_upload(parent, name, b"", conflict=conflict)
        await self._report(0, 0)
        return TransferResult(
            item=dest_item,
            bytes_moved=0,
            verified=True,
            hash_note="empty file",
            resumed=False,
            strategy="empty",
        )

    async def _transfer_small(
        self, item: RemoteItem, parent: str, name: str, conflict: str, verify: bool
    ) -> TransferResult:
        """Under 4 MiB a single PUT beats the three calls a session costs."""
        hashes = Hashers()
        buffer = bytearray()
        async for data in self.source.open_stream(item):
            hashes.update(data)
            buffer.extend(data)
            await self._report(len(buffer), item.size or len(buffer))

        dest_item = await self.dest.simple_upload(
            parent, name, bytes(buffer), conflict=conflict
        )
        verified, note = verify_transfer(item, hashes if verify else None, dest_item)
        if not verify:
            note = "verification disabled"
        return TransferResult(
            item=dest_item,
            bytes_moved=hashes.size,
            verified=verified,
            hash_note=note,
            resumed=False,
            strategy="simple",
        )

    async def _transfer_exported(
        self,
        item: RemoteItem,
        parent: str,
        name: str,
        export_mime: str,
        conflict: str,
        verify: bool,
    ) -> TransferResult:
        """Native Google documents: convert, then upload the result.

        This is the one path that holds a whole file in memory, and it is
        unavoidable - Graph needs the total size in ``Content-Range`` and an
        export's size is not known until the export has arrived. Google caps
        exports at 10 MB, so the buffer is bounded.
        """
        if not export_mime:
            name_only, export_mime = self.source.plan_transfer_name(item)
            name = name or name_only
        if not export_mime:  # pragma: no cover - plan_transfer_name raises instead
            raise SkipFile(f"{item.name!r} has no export format")

        data = await self.source.export_to_memory(item, export_mime)
        hashes = Hashers()
        hashes.update(data)
        total = len(data)
        await self._report(0, total)

        if total == 0:
            dest_item = await self.dest.simple_upload(parent, name, b"", conflict=conflict)
        elif total <= self.settings.simple_upload_max:
            dest_item = await self.dest.simple_upload(parent, name, data, conflict=conflict)
        else:
            session = await self.dest.create_upload_session(
                parent, name, size=total, conflict=conflict
            )
            dest_item = await self._push_buffer(session.url, data, total)

        await self._report(total, total)
        # A converted file is a different artifact, so Drive's own checksums
        # (if any) do not apply; only the destination side is comparable.
        converted = RemoteItem(id=item.id, name=name, mime_type=export_mime, size=total)
        verified, note = verify_transfer(converted, hashes if verify else None, dest_item)
        if verify:
            note = f"{note}; converted from {item.mime_type} so Drive's checksum does not apply"
        else:
            note = "verification disabled"
        return TransferResult(
            item=dest_item,
            bytes_moved=total,
            verified=verified,
            hash_note=note,
            resumed=False,
            strategy="export",
        )

    async def _push_buffer(self, session_url: str, data: bytes, total: int) -> RemoteItem | None:
        """Upload an in-memory buffer through an existing session."""
        chunk = self.chunk_size
        offset = 0
        dest_item: RemoteItem | None = None
        while offset < total:
            piece = data[offset : offset + chunk]
            dest_item = await self.dest.put_chunk(session_url, piece, offset, total)
            offset += len(piece)
            await self._report(offset, total)
        return dest_item

    async def _transfer_streaming(
        self,
        item: RemoteItem,
        parent: str,
        name: str,
        conflict: str,
        resume_url: str | None,
        verify: bool,
    ) -> TransferResult:
        """The real relay: read a chunk, write a chunk, keep nothing."""
        total = item.size or 0
        session_url = resume_url
        resumed = False

        if session_url:
            # Make sure the stored session is still alive before trusting it.
            offset = await self.dest.session_next_offset(session_url, total)
            if offset is None:
                # The session is either already committed or expired. Check for
                # a finished file before sending anything: a process that died
                # between Graph's commit and our own bookkeeping would otherwise
                # upload the whole file again and, under the "rename" policy,
                # leave a duplicate behind.
                existing = await self.dest.find_child(parent, name)
                if existing is not None and existing.size == total:
                    log.info("%r is already complete in the destination; nothing to send", name)
                    return TransferResult(
                        item=existing,
                        bytes_moved=total,
                        verified=None,
                        hash_note=(
                            "already complete in the destination, matched on size; "
                            "no bytes were re-sent, so no hash was computed"
                        ),
                        resumed=True,
                        strategy="stream",
                    )
                log.info("stored upload session for %r is gone; starting a fresh one", name)
                session_url = None
            else:
                resumed = offset > 0

        if not session_url:
            session = await self.dest.create_upload_session(
                parent, name, size=total, conflict=conflict
            )
            session_url = session.url
        await self._announce_session(session_url)

        attempt = 0
        max_attempts = max(self.settings.max_retries, 1)
        hashes: Hashers | None = None

        while True:
            offset = await self.dest.session_next_offset(session_url, total)

            if offset is None:
                # Graph says the session is finished or gone. If the file
                # landed, we are done; otherwise start a new session.
                existing = await self.dest.find_child(parent, name)
                if existing is not None and existing.size == total:
                    verified, note = verify_transfer(item, hashes if verify else None, existing)
                    if hashes is None:
                        note = "hashes not computed (upload completed on an earlier attempt)"
                    return TransferResult(
                        item=existing,
                        bytes_moved=total,
                        verified=verified,
                        hash_note=note,
                        resumed=True,
                        strategy="stream",
                    )
                attempt += 1
                if attempt > max_attempts:
                    raise ProviderError(
                        f"upload session for {name!r} kept disappearing after "
                        f"{max_attempts} attempts"
                    )
                session = await self.dest.create_upload_session(
                    parent, name, size=total, conflict=conflict
                )
                session_url = session.url
                await self._announce_session(session_url)
                continue

            if offset >= total:
                # Everything is uploaded; the commit response was simply lost.
                existing = await self.dest.find_child(parent, name)
                verified, note = verify_transfer(item, hashes if verify else None, existing)
                if hashes is None:
                    note = "hashes not computed (nothing left to send)"
                return TransferResult(
                    item=existing,
                    bytes_moved=total,
                    verified=verified,
                    hash_note=note,
                    resumed=True,
                    strategy="stream",
                )

            if offset > 0:
                resumed = True
            # Hashes are only meaningful over a complete pass from byte zero.
            hashes = Hashers() if offset == 0 else None

            try:
                dest_item = await self._pump(item, session_url, offset, total, hashes)
            except ProviderError as exc:
                if not exc.retryable or attempt >= max_attempts:
                    raise
                attempt += 1
                delay = min(2.0 ** attempt, 30.0)
                log.warning(
                    "%r: the attempt that began at byte %d of %d failed (%s); "
                    "asking Graph how much it holds and retrying %d/%d in %.0fs",
                    name, offset, total, exc, attempt, max_attempts, delay,
                )
                await asyncio.sleep(delay)
                continue

            if dest_item is None:
                # The final PUT should have returned the item. Fetch it so
                # verification has something to compare against.
                dest_item = await self.dest.find_child(parent, name)

            verified, note = verify_transfer(item, hashes if verify else None, dest_item)
            if not verify:
                note = "verification disabled"
            elif hashes is None:
                note = "hashes not computed (transfer resumed mid-file)"
            return TransferResult(
                item=dest_item,
                bytes_moved=total,
                verified=verified,
                hash_note=note,
                resumed=resumed,
                strategy="stream",
            )

    async def _pump(
        self,
        item: RemoteItem,
        session_url: str,
        start: int,
        total: int,
        hashes: Hashers | None,
    ) -> RemoteItem | None:
        """Stream from ``start`` to the end, one chunk in flight at a time."""
        chunk_size = self.chunk_size
        buffer = bytearray()
        offset = start
        dest_item: RemoteItem | None = None

        async for data in self.source.open_stream(item, start=start):
            if offset + len(buffer) + len(data) > total:
                # The file grew, or Drive sent more than it advertised. Pushing
                # past the declared size would corrupt the upload.
                raise ProviderError(
                    f"{item.name!r} produced more than the {total} bytes Drive "
                    f"reported; it was probably modified mid-transfer. Re-run "
                    f"the job to pick up the new version."
                )
            if hashes is not None:
                hashes.update(data)
            buffer.extend(data)

            while len(buffer) >= chunk_size:
                piece = bytes(buffer[:chunk_size])
                del buffer[:chunk_size]
                dest_item = await self.dest.put_chunk(session_url, piece, offset, total)
                offset += len(piece)
                await self._report(offset, total)

        if buffer:
            # Check before sending: a short final buffer is a truncated source,
            # not a legal last chunk, and Graph's error for it ("not a multiple
            # of 320 KiB") would hide the real cause.
            if offset + len(buffer) != total:
                raise ProviderError(
                    f"{item.name!r} ended at {offset + len(buffer)} bytes but Drive "
                    f"reported {total}; the file was probably modified mid-transfer. "
                    f"Re-run the job to pick up the new version."
                )
            piece = bytes(buffer)
            buffer.clear()
            dest_item = await self.dest.put_chunk(session_url, piece, offset, total)
            offset += len(piece)
            await self._report(offset, total)

        if offset != total:
            raise ProviderError(
                f"{item.name!r} ended at {offset} bytes but Drive reported {total}; "
                f"the file was probably modified mid-transfer. Re-run the job to "
                f"pick up the new version."
            )
        return dest_item

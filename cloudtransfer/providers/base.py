"""Shared provider vocabulary.

The transfer engine only ever talks to these two interfaces, which keeps the
relay itself free of Google- and Microsoft-specific quirks and makes a third
provider a matter of writing one adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Returns a valid access token, refreshing it first if necessary.
TokenGetter = Callable[[], Awaitable[str]]


@dataclass(slots=True)
class RemoteItem:
    """A file or folder on either side, normalized."""

    id: str
    name: str
    mime_type: str = ""
    size: int | None = None
    is_folder: bool = False
    md5: str | None = None
    sha256: str | None = None
    sha1: str | None = None
    quick_xor: str | None = None
    modified: str | None = None
    parent_id: str | None = None
    web_url: str | None = None

    # Google Drive shortcuts point at another item instead of holding content.
    shortcut_target_id: str | None = None
    shortcut_target_mime: str | None = None

    can_download: bool = True
    trashed: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_shortcut(self) -> bool:
        return self.shortcut_target_id is not None


@dataclass(slots=True)
class UploadSession:
    """A resumable upload in progress on the destination."""

    url: str
    expires_at: float | None = None


@runtime_checkable
class SourceProvider(Protocol):
    """Read-only side of a transfer."""

    async def get_item(self, item_id: str) -> RemoteItem: ...

    async def list_children(self, folder_id: str) -> list[RemoteItem]: ...

    async def resolve_shortcut(self, item: RemoteItem) -> RemoteItem: ...

    def plan_transfer_name(self, item: RemoteItem) -> tuple[str, str | None]:
        """Return (filename, export_mime). ``export_mime`` is set for formats
        that have no bytes of their own and must be converted on the fly."""
        ...

    def open_stream(self, item: RemoteItem, *, start: int = 0) -> AsyncIterator[bytes]:
        """Yield the item's bytes, optionally resuming from ``start``."""
        ...

    async def export_to_memory(self, item: RemoteItem, export_mime: str) -> bytes:
        """Convert a native document and return it whole.

        Used only where the size cannot be known in advance, which rules out a
        streamed upload.
        """
        ...

    async def close(self) -> None: ...


@runtime_checkable
class DestProvider(Protocol):
    """Write side of a transfer."""

    async def get_item(self, item_id: str) -> RemoteItem: ...

    async def list_children(self, folder_id: str) -> list[RemoteItem]: ...

    async def find_child(self, parent_id: str, name: str) -> RemoteItem | None: ...

    async def ensure_folder(self, parent_id: str, name: str) -> RemoteItem: ...

    async def create_upload_session(
        self, parent_id: str, name: str, *, size: int, conflict: str
    ) -> UploadSession: ...

    async def session_next_offset(self, session_url: str) -> int | None:
        """Bytes the destination already holds, or ``None`` if the session is
        gone (expired, cancelled, or already committed)."""
        ...

    async def put_chunk(
        self, session_url: str, data: bytes, offset: int, total: int
    ) -> RemoteItem | None:
        """Upload one chunk. Returns the finished item on the last chunk."""
        ...

    async def cancel_session(self, session_url: str) -> None: ...

    async def simple_upload(
        self, parent_id: str, name: str, data: bytes, *, conflict: str
    ) -> RemoteItem: ...

    async def delete_item(self, item_id: str) -> None: ...

    async def close(self) -> None: ...

"""OneDrive / Microsoft Graph destination adapter.

The important constraints Graph imposes on an upload session:

* Every chunk except the last must be a multiple of 320 KiB. Graph rejects
  anything else outright, which is why ``CT_CHUNK_SIZE`` is validated at
  startup rather than discovered at runtime.
* ``Content-Range`` must carry the total file size, so a stream of unknown
  length cannot be uploaded this way. That is what forces native Google
  documents down the buffered path.
* The ``uploadUrl`` is already authenticated. Sending an ``Authorization``
  header with it is wrong and can fail the request, so these calls
  deliberately go out bare.
* Graph is authoritative about how much it has received. After any failure the
  engine asks the session for ``nextExpectedRanges`` rather than trusting its
  own counter, which is what makes resume safe.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx

from ..config import ONEDRIVE_CHUNK_MULTIPLE, Settings, get_settings
from ..errors import NotFoundError, ProviderError, SkipFile
from ..http import build_client, classify, request_with_retry
from .base import RemoteItem, TokenGetter, UploadSession

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"

ITEM_SELECT = (
    "id,name,size,file,folder,package,remoteItem,lastModifiedDateTime,webUrl,"
    "parentReference"
)

# How much we pull off the socket at a time when reading a file out of
# OneDrive. Independent of the destination's chunk size; these bytes just
# accumulate in the relay buffer.
DOWNLOAD_READ_SIZE = 256 * 1024

# Formats stored as a package rather than a file. A OneNote notebook has no
# single stream to download, so it cannot be copied byte for byte.
UNDOWNLOADABLE_PACKAGES = {
    "oneNote": "OneNote notebooks are stored as a package and cannot be downloaded",
}


def _encode_path(name: str) -> str:
    """Percent-encode one path segment for Graph's ``items/{id}:/{path}:`` form."""
    return quote(name, safe="")


def _to_item(payload: dict) -> RemoteItem:
    file_facet = payload.get("file") or {}
    hashes = file_facet.get("hashes") or {}
    parent = payload.get("parentReference") or {}
    package = payload.get("package") or {}
    remote = payload.get("remoteItem") or {}
    extra: dict = {}
    if package:
        extra["package_type"] = package.get("type", "unknown")
    if remote:
        # A shared item that physically lives in someone else's drive.
        extra["remote_drive_id"] = (remote.get("parentReference") or {}).get("driveId")
        extra["remote_item_id"] = remote.get("id")
    return RemoteItem(
        id=payload["id"],
        name=payload.get("name", ""),
        mime_type=file_facet.get("mimeType", ""),
        size=payload.get("size"),
        is_folder="folder" in payload,
        quick_xor=hashes.get("quickXorHash"),
        sha1=hashes.get("sha1Hash"),
        sha256=hashes.get("sha256Hash"),
        modified=payload.get("lastModifiedDateTime"),
        parent_id=parent.get("id"),
        web_url=payload.get("webUrl"),
        is_folder_hint=bool(remote.get("folder")) if remote else False,
        extra=extra,
    )


class OneDriveDest:
    """Read/write access to one OneDrive."""

    # Graph rejects any non-final chunk that is not a multiple of 320 KiB.
    chunk_multiple = ONEDRIVE_CHUNK_MULTIPLE

    def __init__(
        self,
        token_getter: TokenGetter,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token_getter
        self.settings = settings or get_settings()
        self._client = client or build_client()
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token()}", **extra}

    async def _json(self, method: str, path: str, **kw) -> dict:
        response = await request_with_retry(
            self._client,
            method,
            f"{GRAPH}{path}",
            headers=await self._headers(),
            max_retries=self.settings.max_retries,
            **kw,
        )
        if not response.content:
            return {}
        return response.json()

    # -- metadata -----------------------------------------------------------

    async def get_drive(self) -> dict:
        return await self._json("GET", "/me/drive")

    async def get_quota(self) -> tuple[int | None, int | None]:
        """Return ``(remaining, total)`` bytes, where Graph reports them."""
        drive = await self.get_drive()
        quota = drive.get("quota") or {}
        return quota.get("remaining"), quota.get("total")

    async def get_root(self) -> RemoteItem:
        payload = await self._json("GET", f"/me/drive/root?$select={ITEM_SELECT}")
        return _to_item(payload)

    async def get_item(self, item_id: str) -> RemoteItem:
        payload = await self._json("GET", f"/me/drive/items/{item_id}?$select={ITEM_SELECT}")
        return _to_item(payload)

    async def list_children(self, folder_id: str) -> list[RemoteItem]:
        items: list[RemoteItem] = []
        url = f"{GRAPH}/me/drive/items/{folder_id}/children?$select={ITEM_SELECT}&$top=200"
        while url:
            response = await request_with_retry(
                self._client,
                "GET",
                url,
                headers=await self._headers(),
                max_retries=self.settings.max_retries,
            )
            payload = response.json()
            items.extend(_to_item(entry) for entry in payload.get("value", []))
            url = payload.get("@odata.nextLink", "")
        return items

    async def find_child(self, parent_id: str, name: str) -> RemoteItem | None:
        """Look up one child by name, or ``None`` if it is not there."""
        path = f"/me/drive/items/{parent_id}:/{_encode_path(name)}:?$select={ITEM_SELECT}"
        try:
            return _to_item(await self._json("GET", path))
        except NotFoundError:
            return None

    async def ensure_folder(self, parent_id: str, name: str) -> RemoteItem:
        """Return the named subfolder, creating it only if absent.

        Idempotent on purpose: re-running a job must reuse the tree it built
        last time instead of piling up ``name (1)``, ``name (2)`` folders.
        """
        existing = await self.find_child(parent_id, name)
        if existing is not None:
            if existing.is_folder:
                return existing
            # A file of that name is in the way; let Graph pick a free name.
            log.warning("%r exists as a file in the destination; creating a renamed folder", name)
            body = {
                "name": name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "rename",
            }
            return _to_item(
                await self._json(
                    "POST", f"/me/drive/items/{parent_id}/children", json=body
                )
            )

        body = {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        try:
            return _to_item(
                await self._json("POST", f"/me/drive/items/{parent_id}/children", json=body)
            )
        except ProviderError as exc:
            if exc.status == 409:
                # Created by a parallel task between our check and our POST.
                found = await self.find_child(parent_id, name)
                if found is not None:
                    return found
            raise

    # -- uploads ------------------------------------------------------------

    async def create_upload_session(
        self, parent_id: str, name: str, *, size: int, conflict: str = "rename"
    ) -> UploadSession:
        body = {
            "item": {
                "@microsoft.graph.conflictBehavior": conflict,
                "name": name,
            },
            # Declaring the size lets Graph reject an over-quota upload now
            # rather than after we have pushed gigabytes at it.
            "fileSize": size,
        }
        path = f"/me/drive/items/{parent_id}:/{_encode_path(name)}:/createUploadSession"
        payload = await self._json("POST", path, json=body)
        url = payload.get("uploadUrl")
        if not url:  # pragma: no cover - Graph always returns one on success
            raise ProviderError(f"Graph returned no uploadUrl for {name!r}")
        return UploadSession(url=url, expires_at=None)

    async def session_next_offset(self, session_url: str, total: int = 0) -> int | None:
        """Ask Graph how many bytes it already holds.

        ``total`` is accepted for a uniform interface and ignored: Graph
        reports progress in the body, while Google needs the size restated.

        Returns the next byte offset it expects, or ``None`` if the session no
        longer exists, in which case the file has to start over.
        """
        try:
            response = await request_with_retry(
                self._client, "GET", session_url, max_retries=2
            )
        except NotFoundError:
            return None
        except ProviderError as exc:
            if exc.status in (404, 410, 409):
                return None
            raise

        payload = response.json()
        ranges = payload.get("nextExpectedRanges") or []
        if not ranges:
            # Empty means Graph considers the upload finished.
            return None
        first = str(ranges[0]).split("-", 1)[0]
        try:
            return int(first)
        except ValueError:  # pragma: no cover - defensive
            return None

    async def put_chunk(
        self, session_url: str, data: bytes, offset: int, total: int
    ) -> RemoteItem | None:
        """Upload one chunk; returns the finished item on the final chunk.

        No ``Authorization`` header: the session URL is pre-authenticated.
        """
        if not data:
            raise ProviderError("refusing to upload an empty chunk")

        end = offset + len(data) - 1
        is_last = end == total - 1
        if not is_last and len(data) % ONEDRIVE_CHUNK_MULTIPLE != 0:
            raise ProviderError(
                f"chunk of {len(data)} bytes is not a multiple of "
                f"{ONEDRIVE_CHUNK_MULTIPLE}; Graph would reject it"
            )

        headers = {
            "Content-Range": f"bytes {offset}-{end}/{total}",
            "Content-Length": str(len(data)),
        }
        response = await request_with_retry(
            self._client,
            "PUT",
            session_url,
            content=data,
            headers=headers,
            max_retries=self.settings.max_retries,
        )

        if response.status_code in (200, 201) and response.content:
            return _to_item(response.json())
        return None

    async def cancel_session(self, session_url: str) -> None:
        try:
            await request_with_retry(self._client, "DELETE", session_url, max_retries=2)
        except ProviderError as exc:  # pragma: no cover - best effort
            log.debug("could not cancel upload session: %s", exc)

    async def simple_upload(
        self, parent_id: str, name: str, data: bytes, *, conflict: str = "rename"
    ) -> RemoteItem:
        """Single-request upload, for small files and exported documents."""
        path = (
            f"/me/drive/items/{parent_id}:/{_encode_path(name)}:/content"
            f"?@microsoft.graph.conflictBehavior={conflict}"
        )
        response = await request_with_retry(
            self._client,
            "PUT",
            f"{GRAPH}{path}",
            content=data,
            headers=await self._headers(**{"Content-Type": "application/octet-stream"}),
            max_retries=self.settings.max_retries,
        )
        return _to_item(response.json())

    async def delete_item(self, item_id: str) -> None:
        await self._json("DELETE", f"/me/drive/items/{item_id}")


class OneDriveSource:
    """Read-only OneDrive access, for transfers that start here.

    Simpler than the Drive source in one respect: every OneDrive file is real
    bytes, so there is no export step and no format conversion. The one
    exception is a package - a OneNote notebook - which has no single stream
    to download and is reported as skipped.
    """

    def __init__(
        self,
        token_getter: TokenGetter,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token_getter
        self.settings = settings or get_settings()
        self._client = client or build_client()
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token()}", **extra}

    async def _json(self, method: str, path: str, **kw) -> dict:
        response = await request_with_retry(
            self._client,
            method,
            f"{GRAPH}{path}",
            headers=await self._headers(),
            max_retries=self.settings.max_retries,
            **kw,
        )
        if not response.content:
            return {}
        return response.json()

    # -- metadata -----------------------------------------------------------

    async def get_root(self) -> RemoteItem:
        payload = await self._json("GET", f"/me/drive/root?$select={ITEM_SELECT}")
        item = _to_item(payload)
        item.name = item.name or "OneDrive"
        return item

    async def get_item(self, item_id: str) -> RemoteItem:
        if item_id in ("root", ""):
            return await self.get_root()
        return _to_item(await self._json("GET", f"/me/drive/items/{item_id}?$select={ITEM_SELECT}"))

    async def list_children(
        self, folder_id: str, *, include_trashed: bool = False
    ) -> list[RemoteItem]:
        """List one folder, following pagination to the end.

        ``include_trashed`` is accepted for a uniform interface and ignored:
        Graph does not list deleted items among a folder's children at all.
        """
        if folder_id in ("root", ""):
            folder_id = (await self.get_root()).id

        items: list[RemoteItem] = []
        url = f"{GRAPH}/me/drive/items/{folder_id}/children?$select={ITEM_SELECT}&$top=200"
        while url:
            response = await request_with_retry(
                self._client,
                "GET",
                url,
                headers=await self._headers(),
                max_retries=self.settings.max_retries,
            )
            payload = response.json()
            items.extend(_to_item(entry) for entry in payload.get("value", []))
            url = payload.get("@odata.nextLink", "")
        return items

    async def resolve_shortcut(self, item: RemoteItem) -> RemoteItem:
        """OneDrive has no shortcut type; shared items are returned as-is."""
        return item

    # -- transfer planning --------------------------------------------------

    def plan_transfer_name(self, item: RemoteItem) -> tuple[str, str | None]:
        """Return the destination filename, and never an export format.

        Nothing stored in OneDrive needs converting on the way out, so the
        second element is always ``None``.
        """
        package_type = item.extra.get("package_type")
        if package_type:
            reason = UNDOWNLOADABLE_PACKAGES.get(
                package_type, f"items stored as a '{package_type}' package cannot be downloaded"
            )
            raise SkipFile(reason)
        return item.name, None

    # -- content ------------------------------------------------------------

    async def open_stream(self, item: RemoteItem, *, start: int = 0) -> AsyncIterator[bytes]:
        """Yield the file's bytes, resuming from ``start`` when asked.

        Graph answers ``/content`` with a redirect to a pre-authenticated
        download URL. The client follows it and drops the bearer token on the
        way, which is correct: that URL carries its own authorization.
        """
        if item.extra.get("package_type"):
            raise SkipFile(
                UNDOWNLOADABLE_PACKAGES.get(
                    item.extra["package_type"],
                    "this item is stored as a package and cannot be downloaded",
                )
            )

        headers = await self._headers()
        if start > 0:
            headers["Range"] = f"bytes={start}-"

        url = f"{GRAPH}/me/drive/items/{item.id}/content"
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")[:600]
                raise classify(response, body)

            skip = 0
            if start > 0 and response.status_code != 206:
                # Something between us and Graph dropped the Range header.
                # Correctness first: discard the bytes we already have.
                log.warning(
                    "Range request for %r returned HTTP %d, not 206; discarding %d "
                    "leading bytes instead of risking a corrupt file",
                    item.name, response.status_code, start,
                )
                skip = start

            async for chunk in response.aiter_bytes(DOWNLOAD_READ_SIZE):
                if skip:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk = chunk[skip:]
                    skip = 0
                if chunk:
                    yield chunk

    async def export_to_memory(self, item: RemoteItem, export_mime: str) -> bytes:
        """Never used: OneDrive stores no format that needs converting."""
        raise ProviderError(
            f"{item.name!r} does not need exporting; OneDrive files are copied as they are"
        )

    async def list_shared_drives(self) -> list[RemoteItem]:
        """Nothing analogous to a Drive shared drive is exposed here."""
        return []

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
from urllib.parse import quote

import httpx

from ..config import ONEDRIVE_CHUNK_MULTIPLE, Settings, get_settings
from ..errors import NotFoundError, ProviderError
from ..http import build_client, request_with_retry
from .base import RemoteItem, TokenGetter, UploadSession

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"

ITEM_SELECT = "id,name,size,file,folder,lastModifiedDateTime,webUrl,parentReference"


def _encode_path(name: str) -> str:
    """Percent-encode one path segment for Graph's ``items/{id}:/{path}:`` form."""
    return quote(name, safe="")


def _to_item(payload: dict) -> RemoteItem:
    file_facet = payload.get("file") or {}
    hashes = file_facet.get("hashes") or {}
    parent = payload.get("parentReference") or {}
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
    )


class OneDriveDest:
    """Read/write access to one OneDrive."""

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

    async def session_next_offset(self, session_url: str) -> int | None:
        """Ask Graph how many bytes it already holds.

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

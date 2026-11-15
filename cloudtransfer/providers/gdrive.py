"""Google Drive source adapter.

Three Drive behaviours shape this file:

1. **Native documents have no bytes.** A Docs/Sheets/Slides file returns an
   error from ``?alt=media``; it has to go through ``/export``, which converts
   on Google's side. The converted size is not knowable in advance, so those
   files are the one case that cannot be streamed and must be buffered in RAM.
   Google also caps ``/export`` at 10 MB.
2. **Shortcuts are not files.** They carry a ``targetId`` and must be resolved
   before anything can be read.
3. **Range requests work on ``?alt=media``**, which is what makes a failed
   transfer resumable without starting over.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from ..config import Settings, get_settings
from ..errors import NotFoundError, ProviderError, SkipFile
from ..http import build_client, classify, request_with_retry
from .base import RemoteItem, TokenGetter

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# How much we pull off the socket at a time. Independent of the OneDrive chunk
# size; these bytes just accumulate in the relay buffer.
DOWNLOAD_READ_SIZE = 256 * 1024

FILE_FIELDS = (
    "id,name,mimeType,size,md5Checksum,sha1Checksum,sha256Checksum,modifiedTime,"
    "trashed,capabilities(canDownload),shortcutDetails(targetId,targetMimeType),"
    "webViewLink,parents"
)

# Native Google formats and what they become on OneDrive. Office formats are
# chosen so the result stays editable in OneDrive rather than becoming a PDF.
EXPORT_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "application/vnd.google-apps.spreadsheet": (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "application/vnd.google-apps.presentation": (
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "application/vnd.google-apps.drawing": (".png", "image/png"),
    "application/vnd.google-apps.script": (
        ".json",
        "application/vnd.google-apps.script+json",
    ),
}

# Native formats Google refuses to export in any useful shape.
UNEXPORTABLE = {
    "application/vnd.google-apps.form": "Google Forms cannot be exported by the Drive API",
    "application/vnd.google-apps.site": "Google Sites cannot be exported by the Drive API",
    "application/vnd.google-apps.map": "Google My Maps cannot be exported by the Drive API",
    "application/vnd.google-apps.fusiontable": "Fusion Tables cannot be exported",
    "application/vnd.google-apps.jam": "Jamboard files cannot be exported",
}

# Google's documented ceiling on /export responses.
EXPORT_SIZE_LIMIT = 10 * 1024 * 1024


def _as_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _to_item(payload: dict) -> RemoteItem:
    shortcut = payload.get("shortcutDetails") or {}
    capabilities = payload.get("capabilities") or {}
    parents = payload.get("parents") or []
    return RemoteItem(
        id=payload["id"],
        name=payload.get("name", ""),
        mime_type=payload.get("mimeType", ""),
        size=_as_int(payload.get("size")),
        is_folder=payload.get("mimeType") == FOLDER_MIME,
        md5=payload.get("md5Checksum"),
        sha1=payload.get("sha1Checksum"),
        sha256=payload.get("sha256Checksum"),
        modified=payload.get("modifiedTime"),
        parent_id=parents[0] if parents else None,
        web_url=payload.get("webViewLink"),
        shortcut_target_id=shortcut.get("targetId"),
        shortcut_target_mime=shortcut.get("targetMimeType"),
        can_download=capabilities.get("canDownload", True),
        trashed=bool(payload.get("trashed")),
    )


def is_native(mime_type: str) -> bool:
    return mime_type.startswith("application/vnd.google-apps.")


class GoogleDriveSource:
    """Read-only Google Drive access."""

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

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token()}"}

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{API}{path}",
            params=params,
            headers=await self._headers(),
            max_retries=self.settings.max_retries,
        )
        return response.json()

    # -- metadata -----------------------------------------------------------

    async def get_item(self, item_id: str) -> RemoteItem:
        payload = await self._get_json(
            f"/files/{item_id}",
            {"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        )
        return _to_item(payload)

    async def list_children(
        self, folder_id: str, *, include_trashed: bool = False
    ) -> list[RemoteItem]:
        """List one folder, following pagination to the end."""
        query = f"'{folder_id}' in parents"
        if not include_trashed:
            query += " and trashed = false"

        items: list[RemoteItem] = []
        page_token: str | None = None
        while True:
            params = {
                "q": query,
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "pageSize": "1000",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "orderBy": "folder,name",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = await self._get_json("/files", params)
            items.extend(_to_item(entry) for entry in payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return items

    async def list_shared_drives(self) -> list[RemoteItem]:
        drives: list[RemoteItem] = []
        page_token: str | None = None
        while True:
            params = {"pageSize": "100", "fields": "nextPageToken,drives(id,name)"}
            if page_token:
                params["pageToken"] = page_token
            payload = await self._get_json("/drives", params)
            drives.extend(
                RemoteItem(id=d["id"], name=d.get("name", d["id"]), is_folder=True,
                           mime_type=FOLDER_MIME)
                for d in payload.get("drives", [])
            )
            page_token = payload.get("nextPageToken")
            if not page_token:
                return drives

    async def resolve_shortcut(self, item: RemoteItem) -> RemoteItem:
        """Follow a shortcut to the item it points at."""
        if not item.is_shortcut:
            return item
        try:
            target = await self.get_item(item.shortcut_target_id or "")
        except NotFoundError as exc:
            raise SkipFile(
                f"shortcut {item.name!r} points at an item that is gone or not shared"
            ) from exc
        # Keep the shortcut's own name; that is what the user sees in Drive.
        target.name = item.name
        return target

    # -- transfer planning --------------------------------------------------

    def plan_transfer_name(self, item: RemoteItem) -> tuple[str, str | None]:
        """Return the destination filename and the export MIME type, if any."""
        if not is_native(item.mime_type):
            return item.name, None

        if item.mime_type in UNEXPORTABLE:
            raise SkipFile(UNEXPORTABLE[item.mime_type])

        mapping = EXPORT_MAP.get(item.mime_type)
        if mapping is None:
            raise SkipFile(f"no export format is defined for {item.mime_type}")

        extension, export_mime = mapping
        name = item.name
        if not name.lower().endswith(extension):
            name += extension
        return name, export_mime

    # -- content ------------------------------------------------------------

    async def open_stream(self, item: RemoteItem, *, start: int = 0) -> AsyncIterator[bytes]:
        """Yield the file's bytes, resuming from ``start`` when asked.

        Nothing is buffered beyond one socket read; the caller decides what to
        do with each piece.
        """
        if is_native(item.mime_type):
            raise ProviderError(
                f"{item.name!r} is a native Google format and has no byte stream; "
                f"it must go through export_to_memory()"
            )
        if not item.can_download:
            raise SkipFile(f"this account may not download {item.name!r}")

        params: dict[str, str] = {"alt": "media", "supportsAllDrives": "true"}
        if self.settings.acknowledge_abuse:
            params["acknowledgeAbuse"] = "true"

        headers = await self._headers()
        if start > 0:
            headers["Range"] = f"bytes={start}-"

        url = f"{API}/files/{item.id}"
        async with self._client.stream("GET", url, params=params, headers=headers) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")[:600]
                if response.status_code == 403 and "cannotDownloadAbusiveFile" in body:
                    raise SkipFile(
                        f"Google has flagged {item.name!r} as abusive. Set "
                        f"CT_ACKNOWLEDGE_ABUSE=true to transfer it anyway."
                    )
                raise classify(response, body)

            skip = 0
            if start > 0 and response.status_code != 206:
                # Something between us and Google dropped the Range header.
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
        """Convert a native document and return the whole result.

        Buffered deliberately: Graph needs the total size up front in
        ``Content-Range`` and an export's size is unknown until it arrives.
        """
        url = f"{API}/files/{item.id}/export"
        try:
            response = await request_with_retry(
                self._client,
                "GET",
                url,
                params={"mimeType": export_mime},
                headers=await self._headers(),
                max_retries=self.settings.max_retries,
            )
        except ProviderError as exc:
            if exc.status == 403 and exc.body and "exportSizeLimitExceeded" in exc.body:
                raise SkipFile(
                    f"{item.name!r} is larger than Google's 10 MB export limit; "
                    f"export it manually or keep it in Drive"
                ) from exc
            raise

        data = response.content
        if len(data) > EXPORT_SIZE_LIMIT:  # pragma: no cover - server-side guard
            log.warning("%r exported to %d bytes, above the documented limit", item.name, len(data))
        return data

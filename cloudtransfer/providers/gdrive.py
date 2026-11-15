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

import json
import logging
import uuid
from collections.abc import AsyncIterator

import httpx

from ..config import Settings, get_settings
from ..errors import NotFoundError, ProviderError, SkipFile
from ..http import build_client, classify, request_with_retry
from .base import RemoteItem, TokenGetter, UploadSession

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


# ---------------------------------------------------------------------------
# Destination side
# ---------------------------------------------------------------------------

UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

# Google's resumable upload requires every chunk but the last to be a multiple
# of 256 KiB. OneDrive wants 320 KiB, so the two are deliberately kept as
# per-provider facts rather than one shared constant.
GOOGLE_CHUNK_MULTIPLE = 256 * 1024


def _escape_query_value(value: str) -> str:
    """Escape a value for a Drive ``q`` search string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveDest:
    """Write access to Google Drive, for transfers that land here.

    The resumable protocol differs from Microsoft's in three ways that matter:
    the session is opened with a POST whose ``Location`` header carries the
    upload URL, progress is reported as ``308 Resume Incomplete`` with a
    ``Range`` header rather than a JSON body, and the status query is a PUT
    with an empty body and ``Content-Range: bytes＊/total``.
    """

    chunk_multiple = GOOGLE_CHUNK_MULTIPLE

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
            f"{API}{path}",
            headers=await self._headers(),
            max_retries=self.settings.max_retries,
            **kw,
        )
        if not response.content:
            return {}
        return response.json()

    # -- metadata -----------------------------------------------------------

    async def get_root(self) -> RemoteItem:
        payload = await self._json(
            "GET",
            "/files/root",
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        )
        item = _to_item(payload)
        item.name = item.name or "My Drive"
        return item

    async def get_item(self, item_id: str) -> RemoteItem:
        payload = await self._json(
            "GET",
            f"/files/{item_id}",
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        )
        return _to_item(payload)

    async def list_children(self, folder_id: str) -> list[RemoteItem]:
        items: list[RemoteItem] = []
        page_token: str | None = None
        while True:
            params = {
                "q": f"'{_escape_query_value(folder_id)}' in parents and trashed = false",
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "pageSize": "1000",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "orderBy": "folder,name",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = await self._json("GET", "/files", params=params)
            items.extend(_to_item(entry) for entry in payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return items

    async def find_child(self, parent_id: str, name: str) -> RemoteItem | None:
        """Look up one child by name, or ``None`` if it is not there.

        Drive permits duplicates, so this can match more than one file; the
        first is returned, which is what a conflict check wants to know about.
        """
        query = (
            f"'{_escape_query_value(parent_id)}' in parents "
            f"and name = '{_escape_query_value(name)}' and trashed = false"
        )
        payload = await self._json(
            "GET",
            "/files",
            params={
                "q": query,
                "fields": f"files({FILE_FIELDS})",
                "pageSize": "10",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        files = payload.get("files") or []
        return _to_item(files[0]) if files else None

    async def ensure_folder(self, parent_id: str, name: str) -> RemoteItem:
        """Return the named subfolder, creating it only if absent."""
        existing = await self.find_child(parent_id, name)
        if existing is not None and existing.is_folder:
            return existing

        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        payload = await self._json(
            "POST",
            "/files",
            json=body,
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        )
        return _to_item(payload)

    # -- uploads ------------------------------------------------------------

    async def create_upload_session(
        self, parent_id: str, name: str, *, size: int, conflict: str = "rename"
    ) -> UploadSession:
        """Open a resumable upload and return the URL Google hands back."""
        replace_id: str | None = None
        if conflict == "replace":
            existing = await self.find_child(parent_id, name)
            if existing is not None and not existing.is_folder:
                replace_id = existing.id

        headers = await self._headers(
            **{
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
            }
        )
        if replace_id:
            # Updating content keeps the file's ID, sharing and revisions.
            url = f"{UPLOAD_API}/files/{replace_id}"
            method = "PATCH"
            body: dict = {"name": name}
        else:
            url = f"{UPLOAD_API}/files"
            method = "POST"
            body = {"name": name, "parents": [parent_id]}

        response = await request_with_retry(
            self._client,
            method,
            url,
            params={"uploadType": "resumable", "supportsAllDrives": "true"},
            json=body,
            headers=headers,
            max_retries=self.settings.max_retries,
            follow_redirects=False,
        )
        location = response.headers.get("Location") or response.headers.get("location")
        if not location:  # pragma: no cover - Google always returns one
            raise ProviderError(f"Google returned no upload URL for {name!r}")
        return UploadSession(url=location)

    @staticmethod
    def _offset_from_range(header: str | None) -> int:
        """``Range: bytes=0-262143`` means 262144 bytes are in hand."""
        if not header:
            return 0
        _, _, span = header.partition("=")
        _, _, end = span.partition("-")
        try:
            return int(end) + 1
        except ValueError:  # pragma: no cover - defensive
            return 0

    async def session_next_offset(self, session_url: str, total: int) -> int | None:
        """Ask Google how much it already holds.

        Returns the next byte offset it expects, or ``None`` when the session
        is finished or gone, in which case the caller starts a new one.
        """
        try:
            response = await request_with_retry(
                self._client,
                "PUT",
                session_url,
                content=b"",
                headers=await self._headers(
                    **{"Content-Range": f"bytes */{total}", "Content-Length": "0"}
                ),
                max_retries=2,
                follow_redirects=False,
                retry_on=frozenset({429, 500, 502, 503, 504}),
            )
        except NotFoundError:
            return None
        except ProviderError as exc:
            # 404/410 mean the session expired; 400 can mean it was completed.
            if exc.status in (400, 404, 410):
                return None
            raise

        if response.status_code in (200, 201):
            return None  # already committed
        if response.status_code == 308:
            return self._offset_from_range(response.headers.get("Range"))
        return None

    async def put_chunk(
        self, session_url: str, data: bytes, offset: int, total: int
    ) -> RemoteItem | None:
        """Upload one chunk; returns the finished file on the last one."""
        if not data:
            raise ProviderError("refusing to upload an empty chunk")

        end = offset + len(data) - 1
        is_last = end == total - 1
        if not is_last and len(data) % GOOGLE_CHUNK_MULTIPLE != 0:
            raise ProviderError(
                f"chunk of {len(data)} bytes is not a multiple of "
                f"{GOOGLE_CHUNK_MULTIPLE}; Google would reject it"
            )

        response = await request_with_retry(
            self._client,
            "PUT",
            session_url,
            content=data,
            headers=await self._headers(
                **{
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                    "Content-Length": str(len(data)),
                }
            ),
            max_retries=self.settings.max_retries,
            follow_redirects=False,
        )

        if response.status_code in (200, 201) and response.content:
            return _to_item(response.json())
        return None

    async def cancel_session(self, session_url: str) -> None:
        try:
            await request_with_retry(
                self._client, "DELETE", session_url, max_retries=2, follow_redirects=False
            )
        except ProviderError as exc:  # pragma: no cover - best effort
            log.debug("could not cancel upload session: %s", exc)

    async def simple_upload(
        self, parent_id: str, name: str, data: bytes, *, conflict: str = "rename"
    ) -> RemoteItem:
        """Single-request upload, for small files and exported documents.

        Drive's one-shot form is ``multipart/related``: a JSON metadata part
        followed by the bytes. It is built by hand because the standard
        multipart encoders all produce ``multipart/form-data`` instead.
        """
        replace_id: str | None = None
        if conflict == "replace":
            existing = await self.find_child(parent_id, name)
            if existing is not None and not existing.is_folder:
                replace_id = existing.id

        metadata: dict = {"name": name}
        if not replace_id:
            metadata["parents"] = [parent_id]

        boundary = f"cloudtransfer-{uuid.uuid4().hex}"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
                json.dumps(metadata).encode("utf-8"),
                f"\r\n--{boundary}\r\n".encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                data,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )

        url = f"{UPLOAD_API}/files/{replace_id}" if replace_id else f"{UPLOAD_API}/files"
        response = await request_with_retry(
            self._client,
            "PATCH" if replace_id else "POST",
            url,
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": FILE_FIELDS,
            },
            content=body,
            headers=await self._headers(
                **{"Content-Type": f"multipart/related; boundary={boundary}"}
            ),
            max_retries=self.settings.max_retries,
        )
        return _to_item(response.json())

    async def delete_item(self, item_id: str) -> None:
        await self._json(
            "DELETE", f"/files/{item_id}", params={"supportsAllDrives": "true"}
        )

    async def get_quota(self) -> tuple[int | None, int | None]:
        """Return ``(remaining, total)`` bytes, where Drive reports them."""
        payload = await self._json(
            "GET", "/about", params={"fields": "storageQuota"}
        )
        quota = payload.get("storageQuota") or {}
        limit = _as_int(quota.get("limit"))
        usage = _as_int(quota.get("usage"))
        if limit is None:
            return None, None  # unlimited (pooled Workspace storage)
        remaining = max(0, limit - (usage or 0))
        return remaining, limit

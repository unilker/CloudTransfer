"""The real Google Drive adapter, against mocked HTTP.

These cover the parts the in-memory fake cannot: the actual request shapes,
Range handling, and how Drive's specific error bodies are classified.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from cloudtransfer.config import Settings
from cloudtransfer.errors import NotFoundError, ProviderError, SkipFile
from cloudtransfer.providers.base import RemoteItem
from cloudtransfer.providers.gdrive import (
    EXPORT_MAP,
    FOLDER_MIME,
    UNEXPORTABLE,
    GoogleDriveSource,
    is_native,
)

API = "https://www.googleapis.com/drive/v3"


async def _token() -> str:
    return "test-access-token"


@pytest.fixture
def source(tmp_path) -> GoogleDriveSource:
    settings = Settings(data_dir=tmp_path, secret_key="k", max_retries=1)
    return GoogleDriveSource(_token, settings)


async def _drain(agen) -> bytes:
    out = bytearray()
    async for piece in agen:
        out.extend(piece)
    return bytes(out)


# ------------------------------------------------------------------ metadata


@respx.mock
async def test_get_item_maps_drive_fields(source):
    respx.get(f"{API}/files/abc").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "abc",
                "name": "photo.jpg",
                "mimeType": "image/jpeg",
                "size": "2048",
                "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "parents": ["parent1"],
                "capabilities": {"canDownload": True},
                "webViewLink": "https://drive.google.com/file/d/abc",
            },
        )
    )
    item = await source.get_item("abc")
    assert item.name == "photo.jpg"
    assert item.size == 2048  # Drive sends size as a string
    assert item.md5 == "d41d8cd98f00b204e9800998ecf8427e"
    assert item.parent_id == "parent1"
    assert item.is_folder is False
    await source.close()


@respx.mock
async def test_missing_item_raises_not_found(source):
    respx.get(f"{API}/files/gone").mock(
        return_value=httpx.Response(404, json={"error": {"message": "File not found"}})
    )
    with pytest.raises(NotFoundError):
        await source.get_item("gone")
    await source.close()


@respx.mock
async def test_list_children_follows_pagination(source):
    route = respx.get(f"{API}/files")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "nextPageToken": "page2",
                "files": [{"id": "1", "name": "a.txt", "mimeType": "text/plain", "size": "1"}],
            },
        ),
        httpx.Response(
            200,
            json={
                "files": [
                    {"id": "2", "name": "sub", "mimeType": FOLDER_MIME},
                    {"id": "3", "name": "b.txt", "mimeType": "text/plain", "size": "2"},
                ]
            },
        ),
    ]
    items = await source.list_children("root")
    assert [i.id for i in items] == ["1", "2", "3"]
    assert items[1].is_folder is True
    assert route.call_count == 2
    # The second request must carry the page token.
    assert "pageToken=page2" in str(route.calls[1].request.url)
    await source.close()


@respx.mock
async def test_list_children_excludes_trash_by_default(source):
    route = respx.get(f"{API}/files").mock(return_value=httpx.Response(200, json={"files": []}))
    await source.list_children("root")
    assert "trashed+%3D+false" in str(route.calls[0].request.url) or \
           "trashed = false" in httpx.URL(str(route.calls[0].request.url)).params["q"]

    await source.list_children("root", include_trashed=True)
    assert "trashed" not in httpx.URL(str(route.calls[1].request.url)).params["q"]
    await source.close()


@respx.mock
async def test_shortcut_resolution_keeps_the_shortcut_name(source):
    respx.get(f"{API}/files/target").mock(
        return_value=httpx.Response(
            200,
            json={"id": "target", "name": "real-name.pdf", "mimeType": "application/pdf",
                  "size": "10"},
        )
    )
    shortcut = RemoteItem(
        id="sc", name="link to thing", shortcut_target_id="target",
        mime_type="application/vnd.google-apps.shortcut",
    )
    resolved = await source.resolve_shortcut(shortcut)
    assert resolved.id == "target"
    assert resolved.name == "link to thing"  # what the user sees in Drive
    assert resolved.size == 10
    await source.close()


@respx.mock
async def test_broken_shortcut_is_skipped(source):
    respx.get(f"{API}/files/missing").mock(return_value=httpx.Response(404, json={}))
    shortcut = RemoteItem(id="sc", name="dead link", shortcut_target_id="missing")
    with pytest.raises(SkipFile, match="gone or not shared"):
        await source.resolve_shortcut(shortcut)
    await source.close()


# ------------------------------------------------------------- naming/export


@pytest.mark.parametrize(
    "mime,extension",
    [
        ("application/vnd.google-apps.document", ".docx"),
        ("application/vnd.google-apps.spreadsheet", ".xlsx"),
        ("application/vnd.google-apps.presentation", ".pptx"),
        ("application/vnd.google-apps.drawing", ".png"),
    ],
)
def test_native_types_get_an_office_extension(source, mime, extension):
    name, export_mime = source.plan_transfer_name(
        RemoteItem(id="x", name="My file", mime_type=mime)
    )
    assert name == f"My file{extension}"
    assert export_mime == EXPORT_MAP[mime][1]


def test_existing_extension_is_not_doubled(source):
    name, _ = source.plan_transfer_name(
        RemoteItem(id="x", name="Report.docx",
                   mime_type="application/vnd.google-apps.document")
    )
    assert name == "Report.docx"


@pytest.mark.parametrize("mime", sorted(UNEXPORTABLE))
def test_unexportable_types_are_skipped_with_a_reason(source, mime):
    with pytest.raises(SkipFile) as info:
        source.plan_transfer_name(RemoteItem(id="x", name="thing", mime_type=mime))
    assert "cannot be exported" in str(info.value)


def test_binary_files_pass_through_untouched(source):
    name, export_mime = source.plan_transfer_name(
        RemoteItem(id="x", name="archive.zip", mime_type="application/zip")
    )
    assert name == "archive.zip"
    assert export_mime is None


def test_is_native_only_matches_google_formats():
    assert is_native("application/vnd.google-apps.document")
    assert is_native(FOLDER_MIME)
    assert not is_native("application/pdf")
    assert not is_native("")


# ---------------------------------------------------------------- downloading


@respx.mock
async def test_download_streams_without_a_range_header(source):
    payload = b"0123456789" * 100
    route = respx.get(f"{API}/files/abc").mock(
        return_value=httpx.Response(200, content=payload)
    )
    item = RemoteItem(id="abc", name="a.bin", mime_type="application/octet-stream",
                      size=len(payload))
    assert await _drain(source.open_stream(item)) == payload
    assert "Range" not in route.calls[0].request.headers
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-access-token"
    await source.close()


@respx.mock
async def test_resume_sends_a_range_header_and_accepts_206(source):
    payload = b"abcdefghij" * 50
    start = 120
    route = respx.get(f"{API}/files/abc").mock(
        return_value=httpx.Response(
            206,
            content=payload[start:],
            headers={"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"},
        )
    )
    item = RemoteItem(id="abc", name="a.bin", mime_type="application/octet-stream",
                      size=len(payload))
    assert await _drain(source.open_stream(item, start=start)) == payload[start:]
    assert route.calls[0].request.headers["Range"] == f"bytes={start}-"
    await source.close()


@respx.mock
async def test_a_server_that_ignores_range_does_not_corrupt_the_resume(source):
    """A proxy may strip Range. Returning 200 means the body starts at zero,
    so the adapter must drop the bytes the destination already has."""
    payload = bytes(range(256)) * 4
    start = 300
    respx.get(f"{API}/files/abc").mock(return_value=httpx.Response(200, content=payload))
    item = RemoteItem(id="abc", name="a.bin", mime_type="application/octet-stream",
                      size=len(payload))
    assert await _drain(source.open_stream(item, start=start)) == payload[start:]
    await source.close()


@respx.mock
async def test_abusive_file_is_skipped_with_actionable_advice(source):
    respx.get(f"{API}/files/abc").mock(
        return_value=httpx.Response(
            403,
            json={"error": {"errors": [{"reason": "cannotDownloadAbusiveFile"}]}},
        )
    )
    item = RemoteItem(id="abc", name="suspicious.exe", mime_type="application/octet-stream",
                      size=10)
    with pytest.raises(SkipFile, match="CT_ACKNOWLEDGE_ABUSE"):
        await _drain(source.open_stream(item))
    await source.close()


@respx.mock
async def test_acknowledge_abuse_setting_is_sent(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key="k", acknowledge_abuse=True,
                        max_retries=1)
    source = GoogleDriveSource(_token, settings)
    route = respx.get(f"{API}/files/abc").mock(return_value=httpx.Response(200, content=b"x"))
    item = RemoteItem(id="abc", name="a.bin", mime_type="application/octet-stream", size=1)
    await _drain(source.open_stream(item))
    assert "acknowledgeAbuse=true" in str(route.calls[0].request.url)
    await source.close()


async def test_native_formats_have_no_byte_stream(source):
    item = RemoteItem(id="x", name="Doc", mime_type="application/vnd.google-apps.document")
    with pytest.raises(ProviderError, match="no byte stream"):
        await _drain(source.open_stream(item))
    await source.close()


async def test_undownloadable_file_is_skipped(source):
    item = RemoteItem(id="x", name="locked.pdf", mime_type="application/pdf",
                      size=1, can_download=False)
    with pytest.raises(SkipFile, match="may not download"):
        await _drain(source.open_stream(item))
    await source.close()


# -------------------------------------------------------------------- exports


@respx.mock
async def test_export_returns_the_converted_bytes(source):
    body = b"PK\x03\x04docx-bytes"
    route = respx.get(f"{API}/files/doc1/export").mock(
        return_value=httpx.Response(200, content=body)
    )
    item = RemoteItem(id="doc1", name="Notes",
                      mime_type="application/vnd.google-apps.document")
    docx = EXPORT_MAP["application/vnd.google-apps.document"][1]
    assert await source.export_to_memory(item, docx) == body
    assert httpx.URL(str(route.calls[0].request.url)).params["mimeType"] == docx
    await source.close()


@respx.mock
async def test_export_over_googles_size_limit_is_skipped(source):
    respx.get(f"{API}/files/doc1/export").mock(
        return_value=httpx.Response(
            403,
            json={"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}},
        )
    )
    item = RemoteItem(id="doc1", name="Huge doc",
                      mime_type="application/vnd.google-apps.document")
    with pytest.raises(SkipFile, match="10 MB export limit"):
        await source.export_to_memory(
            item, EXPORT_MAP["application/vnd.google-apps.document"][1]
        )
    await source.close()


# -------------------------------------------------------------- shared drives


@respx.mock
async def test_shared_drives_are_listed(source):
    respx.get(f"{API}/drives").mock(
        return_value=httpx.Response(
            200, json={"drives": [{"id": "d1", "name": "Team"}, {"id": "d2", "name": "Legal"}]}
        )
    )
    drives = await source.list_shared_drives()
    assert [(d.id, d.name) for d in drives] == [("d1", "Team"), ("d2", "Legal")]
    assert all(d.is_folder for d in drives)
    await source.close()

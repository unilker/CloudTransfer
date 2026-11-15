"""The real Google Drive destination adapter, against mocked HTTP.

Drive's resumable protocol differs from Microsoft's in ways that are easy to
get subtly wrong: the upload URL arrives in a ``Location`` header rather than a
JSON body, progress comes back as ``308`` with a ``Range`` header rather than a
list of expected ranges, and the one-shot upload is ``multipart/related``
rather than a plain PUT.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from cloudtransfer.config import Settings
from cloudtransfer.errors import ProviderError
from cloudtransfer.providers.gdrive import (
    API,
    GOOGLE_CHUNK_MULTIPLE,
    UPLOAD_API,
    GoogleDriveDest,
    _escape_query_value,
)

SESSION_URL = "https://www.googleapis.com/upload/drive/v3/files?upload_id=XYZ123"


async def _token() -> str:
    return "google-access-token"


@pytest.fixture
def dest(tmp_path) -> GoogleDriveDest:
    settings = Settings(data_dir=tmp_path, secret_key="k", max_retries=1)
    return GoogleDriveDest(_token, settings)


def _file(name="a.bin", size=10, file_id="f1"):
    return {
        "id": file_id,
        "name": name,
        "mimeType": "application/octet-stream",
        "size": str(size),
        "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
        "parents": ["p1"],
    }


# -------------------------------------------------------------------- lookups


@respx.mock
async def test_find_child_builds_a_scoped_query(dest):
    route = respx.get(f"{API}/files").mock(
        return_value=httpx.Response(200, json={"files": [_file("report.pdf")]})
    )
    item = await dest.find_child("parent1", "report.pdf")

    assert item is not None and item.name == "report.pdf"
    q = httpx.URL(str(route.calls[0].request.url)).params["q"]
    assert "'parent1' in parents" in q
    assert "name = 'report.pdf'" in q
    assert "trashed = false" in q
    await dest.close()


@respx.mock
async def test_find_child_returns_none_when_absent(dest):
    respx.get(f"{API}/files").mock(return_value=httpx.Response(200, json={"files": []}))
    assert await dest.find_child("parent1", "nothere.bin") is None
    await dest.close()


def test_query_values_are_escaped():
    """An apostrophe in a filename would otherwise break out of the query."""
    assert _escape_query_value("Bob's file.txt") == "Bob\\'s file.txt"
    assert _escape_query_value("back\\slash") == "back\\\\slash"
    assert _escape_query_value("plain.txt") == "plain.txt"


@respx.mock
async def test_a_quoted_name_does_not_corrupt_the_query(dest):
    route = respx.get(f"{API}/files").mock(
        return_value=httpx.Response(200, json={"files": []})
    )
    await dest.find_child("p", "Bob's notes.txt")
    q = httpx.URL(str(route.calls[0].request.url)).params["q"]
    assert "name = 'Bob\\'s notes.txt'" in q
    await dest.close()


@respx.mock
async def test_ensure_folder_reuses_an_existing_one(dest):
    respx.get(f"{API}/files").mock(
        return_value=httpx.Response(
            200,
            json={
                "files": [
                    {
                        "id": "existing",
                        "name": "Photos",
                        "mimeType": "application/vnd.google-apps.folder",
                    }
                ]
            },
        )
    )
    create = respx.post(f"{API}/files")
    folder = await dest.ensure_folder("p1", "Photos")

    assert folder.id == "existing"
    assert folder.is_folder is True
    assert not create.called  # creating again would make a second "Photos"
    await dest.close()


@respx.mock
async def test_ensure_folder_creates_when_missing(dest):
    respx.get(f"{API}/files").mock(return_value=httpx.Response(200, json={"files": []}))
    create = respx.post(f"{API}/files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "new1",
                "name": "Photos",
                "mimeType": "application/vnd.google-apps.folder",
            },
        )
    )
    folder = await dest.ensure_folder("p1", "Photos")

    assert folder.id == "new1"
    body = json.loads(create.calls[0].request.content)
    assert body["mimeType"] == "application/vnd.google-apps.folder"
    assert body["parents"] == ["p1"]
    await dest.close()


# ----------------------------------------------------------- upload sessions


@respx.mock
async def test_the_upload_url_comes_from_the_location_header(dest):
    route = respx.post(f"{UPLOAD_API}/files").mock(
        return_value=httpx.Response(200, headers={"Location": SESSION_URL})
    )
    session = await dest.create_upload_session("p1", "big.bin", size=5_000_000)

    assert session.url == SESSION_URL
    request = route.calls[0].request
    assert request.headers["X-Upload-Content-Length"] == "5000000"
    assert "uploadType=resumable" in str(request.url)
    body = json.loads(request.content)
    assert body == {"name": "big.bin", "parents": ["p1"]}
    await dest.close()


@respx.mock
async def test_a_missing_location_header_is_an_error(dest):
    respx.post(f"{UPLOAD_API}/files").mock(return_value=httpx.Response(200))
    with pytest.raises(ProviderError, match="no upload URL"):
        await dest.create_upload_session("p1", "x.bin", size=1)
    await dest.close()


@respx.mock
async def test_replace_updates_the_existing_file_in_place(dest):
    """Keeping the file ID preserves its sharing links and revision history."""
    respx.get(f"{API}/files").mock(
        return_value=httpx.Response(200, json={"files": [_file("doc.pdf", file_id="old1")]})
    )
    patch = respx.patch(f"{UPLOAD_API}/files/old1").mock(
        return_value=httpx.Response(200, headers={"Location": SESSION_URL})
    )
    post = respx.post(f"{UPLOAD_API}/files")

    session = await dest.create_upload_session("p1", "doc.pdf", size=100, conflict="replace")

    assert session.url == SESSION_URL
    assert patch.called and not post.called
    body = json.loads(patch.calls[0].request.content)
    assert "parents" not in body  # moving it was never asked for
    await dest.close()


@respx.mock
async def test_rename_creates_a_second_file(dest):
    """Drive allows duplicates, so "keep both" needs no lookup at all."""
    lookup = respx.get(f"{API}/files")
    route = respx.post(f"{UPLOAD_API}/files").mock(
        return_value=httpx.Response(200, headers={"Location": SESSION_URL})
    )
    await dest.create_upload_session("p1", "doc.pdf", size=100, conflict="rename")
    assert route.called
    assert not lookup.called
    await dest.close()


# ------------------------------------------------------------------- chunks


@respx.mock
async def test_a_chunk_sends_content_range_and_is_not_final(dest):
    total = GOOGLE_CHUNK_MULTIPLE * 2
    route = respx.put(SESSION_URL).mock(
        return_value=httpx.Response(308, headers={"Range": "bytes=0-262143"})
    )
    data = b"x" * GOOGLE_CHUNK_MULTIPLE

    result = await dest.put_chunk(SESSION_URL, data, 0, total)

    assert result is None  # 308 means more is expected
    request = route.calls[0].request
    assert request.headers["Content-Range"] == f"bytes 0-{GOOGLE_CHUNK_MULTIPLE - 1}/{total}"
    assert request.headers["Authorization"] == "Bearer google-access-token"
    await dest.close()


@respx.mock
async def test_the_final_chunk_returns_the_file(dest):
    total = GOOGLE_CHUNK_MULTIPLE + 50
    respx.put(SESSION_URL).mock(
        return_value=httpx.Response(200, json=_file("done.bin", size=total))
    )
    item = await dest.put_chunk(SESSION_URL, b"y" * 50, GOOGLE_CHUNK_MULTIPLE, total)

    assert item is not None
    assert item.name == "done.bin"
    assert item.size == total
    await dest.close()


async def test_a_chunk_google_would_reject_is_refused_locally(dest):
    """256 KiB multiples, not OneDrive's 320 KiB."""
    with pytest.raises(ProviderError, match="not a multiple"):
        await dest.put_chunk(SESSION_URL, b"z" * 327_680, 0, 10_000_000)
    await dest.close()


@respx.mock
async def test_a_short_final_chunk_is_allowed(dest):
    respx.put(SESSION_URL).mock(
        return_value=httpx.Response(200, json=_file("small.bin", size=1000))
    )
    assert await dest.put_chunk(SESSION_URL, b"q" * 1000, 0, 1000) is not None
    await dest.close()


async def test_an_empty_chunk_is_refused(dest):
    with pytest.raises(ProviderError, match="empty chunk"):
        await dest.put_chunk(SESSION_URL, b"", 0, 100)
    await dest.close()


# ------------------------------------------------------------ resume status


@respx.mock
async def test_progress_is_read_from_the_range_header(dest):
    route = respx.put(SESSION_URL).mock(
        return_value=httpx.Response(308, headers={"Range": "bytes=0-524287"})
    )
    offset = await dest.session_next_offset(SESSION_URL, 1_000_000)

    assert offset == 524_288  # the header is inclusive; the next byte is one on
    request = route.calls[0].request
    assert request.headers["Content-Range"] == "bytes */1000000"
    assert request.content == b""
    await dest.close()


@respx.mock
async def test_no_range_header_means_nothing_arrived_yet(dest):
    respx.put(SESSION_URL).mock(return_value=httpx.Response(308))
    assert await dest.session_next_offset(SESSION_URL, 1000) == 0
    await dest.close()


@respx.mock
async def test_a_completed_upload_reports_none(dest):
    respx.put(SESSION_URL).mock(
        return_value=httpx.Response(200, json=_file("done.bin"))
    )
    assert await dest.session_next_offset(SESSION_URL, 1000) is None
    await dest.close()


@pytest.mark.parametrize("status", [400, 404, 410])
@respx.mock
async def test_an_expired_session_reports_none(dest, status):
    respx.put(SESSION_URL).mock(return_value=httpx.Response(status, json={}))
    assert await dest.session_next_offset(SESSION_URL, 1000) is None
    await dest.close()


@respx.mock
async def test_an_unexpected_status_is_raised(dest):
    respx.put(SESSION_URL).mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(ProviderError):
        await dest.session_next_offset(SESSION_URL, 1000)
    await dest.close()


def test_range_header_parsing():
    assert GoogleDriveDest._offset_from_range("bytes=0-0") == 1
    assert GoogleDriveDest._offset_from_range("bytes=0-262143") == 262_144
    assert GoogleDriveDest._offset_from_range(None) == 0
    assert GoogleDriveDest._offset_from_range("nonsense") == 0


# ------------------------------------------------------------ simple upload


@respx.mock
async def test_simple_upload_sends_multipart_related(dest):
    route = respx.post(f"{UPLOAD_API}/files").mock(
        return_value=httpx.Response(200, json=_file("tiny.txt", size=5))
    )
    item = await dest.simple_upload("p1", "tiny.txt", b"hello")

    assert item.name == "tiny.txt"
    request = route.calls[0].request
    content_type = request.headers["Content-Type"]
    assert content_type.startswith("multipart/related; boundary=")

    boundary = content_type.split("boundary=", 1)[1]
    body = request.content
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert body.rstrip().endswith(f"--{boundary}--".encode())
    assert b'"name": "tiny.txt"' in body
    assert b'"parents": ["p1"]' in body
    assert b"hello" in body
    assert "uploadType=multipart" in str(request.url)
    await dest.close()


@respx.mock
async def test_simple_upload_can_replace_in_place(dest):
    respx.get(f"{API}/files").mock(
        return_value=httpx.Response(200, json={"files": [_file("tiny.txt", file_id="old")]})
    )
    patch = respx.patch(f"{UPLOAD_API}/files/old").mock(
        return_value=httpx.Response(200, json=_file("tiny.txt"))
    )
    await dest.simple_upload("p1", "tiny.txt", b"new", conflict="replace")

    assert patch.called
    assert b'"parents"' not in patch.calls[0].request.content
    await dest.close()


@respx.mock
async def test_an_empty_file_still_sends_both_parts(dest):
    route = respx.post(f"{UPLOAD_API}/files").mock(
        return_value=httpx.Response(200, json=_file("empty.bin", size=0))
    )
    await dest.simple_upload("p1", "empty.bin", b"")
    body = route.calls[0].request.content
    assert body.count(b"Content-Type:") == 2  # metadata part plus media part
    await dest.close()


# -------------------------------------------------------------------- quota


@respx.mock
async def test_quota_is_the_difference_between_limit_and_usage(dest):
    respx.get(f"{API}/about").mock(
        return_value=httpx.Response(
            200, json={"storageQuota": {"limit": "1000000", "usage": "400000"}}
        )
    )
    remaining, total = await dest.get_quota()
    assert remaining == 600_000
    assert total == 1_000_000
    await dest.close()


@respx.mock
async def test_pooled_storage_reports_no_limit(dest):
    """A Workspace account can have no per-user cap at all."""
    respx.get(f"{API}/about").mock(
        return_value=httpx.Response(200, json={"storageQuota": {"usage": "400000"}})
    )
    assert await dest.get_quota() == (None, None)
    await dest.close()


@respx.mock
async def test_an_over_quota_account_reports_zero_not_a_negative(dest):
    respx.get(f"{API}/about").mock(
        return_value=httpx.Response(
            200, json={"storageQuota": {"limit": "100", "usage": "500"}}
        )
    )
    remaining, _ = await dest.get_quota()
    assert remaining == 0
    await dest.close()

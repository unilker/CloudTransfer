"""The real OneDrive adapter, against mocked HTTP.

The focus is the upload-session protocol, where getting a header wrong means
Graph rejects everything, and the pre-authenticated session URL, where sending
the wrong header is a security as well as a correctness problem.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from cloudtransfer.config import ONEDRIVE_CHUNK_MULTIPLE, Settings
from cloudtransfer.errors import NotFoundError, ProviderError
from cloudtransfer.providers.onedrive import GRAPH, OneDriveDest, _encode_path

SESSION_URL = "https://upload.example.invalid/session/abc123"


async def _token() -> str:
    return "ms-access-token"


@pytest.fixture
def dest(tmp_path) -> OneDriveDest:
    settings = Settings(data_dir=tmp_path, secret_key="k", max_retries=1)
    return OneDriveDest(_token, settings)


def _file_payload(name="a.bin", size=10, item_id="o1", qxh="AAAA"):
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "file": {"mimeType": "application/octet-stream", "hashes": {"quickXorHash": qxh}},
        "parentReference": {"id": "parent1"},
    }


# -------------------------------------------------------------------- metadata


@respx.mock
async def test_get_item_maps_graph_fields(dest):
    respx.get(url__regex=rf"{GRAPH}/me/drive/items/o1.*").mock(
        return_value=httpx.Response(200, json=_file_payload(qxh="abc123=="))
    )
    item = await dest.get_item("o1")
    assert item.name == "a.bin"
    assert item.size == 10
    assert item.quick_xor == "abc123=="
    assert item.is_folder is False
    await dest.close()


@respx.mock
async def test_folder_items_are_recognised(dest):
    respx.get(url__regex=rf"{GRAPH}/me/drive/root.*").mock(
        return_value=httpx.Response(
            200, json={"id": "root1", "name": "root", "folder": {"childCount": 3}}
        )
    )
    root = await dest.get_root()
    assert root.is_folder is True
    assert root.id == "root1"
    await dest.close()


@respx.mock
async def test_list_children_follows_odata_next_link(dest):
    first = f"{GRAPH}/me/drive/items/f1/children"
    second = f"{GRAPH}/me/drive/items/f1/children?$skiptoken=XYZ"
    respx.get(url__regex=rf"{first}\?\$select.*").mock(
        return_value=httpx.Response(
            200,
            json={"value": [_file_payload("a.bin", item_id="o1")], "@odata.nextLink": second},
        )
    )
    respx.get(second).mock(
        return_value=httpx.Response(200, json={"value": [_file_payload("b.bin", item_id="o2")]})
    )
    items = await dest.list_children("f1")
    assert [i.id for i in items] == ["o1", "o2"]
    await dest.close()


@respx.mock
async def test_find_child_returns_none_when_absent(dest):
    respx.get(url__regex=rf"{GRAPH}/me/drive/items/f1:/.*").mock(
        return_value=httpx.Response(404, json={"error": {"code": "itemNotFound"}})
    )
    assert await dest.find_child("f1", "nothere.bin") is None
    await dest.close()


@respx.mock
async def test_names_are_percent_encoded_in_the_path(dest):
    route = respx.get(url__regex=rf"{GRAPH}/me/drive/items/f1:/.*").mock(
        return_value=httpx.Response(200, json=_file_payload("odd name.bin"))
    )
    await dest.find_child("f1", "odd name & #1.bin")
    url = str(route.calls[0].request.url)
    assert "odd%20name%20%26%20%231.bin" in url
    await dest.close()


def test_encode_path_escapes_graph_delimiters():
    assert _encode_path("a/b.txt") == "a%2Fb.txt"
    assert _encode_path("q?x=1") == "q%3Fx%3D1"
    assert _encode_path("plain.txt") == "plain.txt"


# --------------------------------------------------------------------- folders


@respx.mock
async def test_ensure_folder_reuses_an_existing_folder(dest):
    respx.get(url__regex=rf"{GRAPH}/me/drive/items/f1:/.*").mock(
        return_value=httpx.Response(
            200, json={"id": "existing", "name": "Photos", "folder": {}}
        )
    )
    create = respx.post(f"{GRAPH}/me/drive/items/f1/children")
    folder = await dest.ensure_folder("f1", "Photos")
    assert folder.id == "existing"
    assert not create.called  # creating it again would make "Photos 1"
    await dest.close()


@respx.mock
async def test_ensure_folder_creates_when_missing(dest):
    respx.get(url__regex=rf"{GRAPH}/me/drive/items/f1:/.*").mock(
        return_value=httpx.Response(404, json={})
    )
    create = respx.post(f"{GRAPH}/me/drive/items/f1/children").mock(
        return_value=httpx.Response(201, json={"id": "new1", "name": "Photos", "folder": {}})
    )
    folder = await dest.ensure_folder("f1", "Photos")
    assert folder.id == "new1"
    body = json.loads(create.calls[0].request.content)
    assert body["name"] == "Photos"
    assert body["folder"] == {}
    # "fail" rather than "rename", so a lost race is detected, not duplicated.
    assert body["@microsoft.graph.conflictBehavior"] == "fail"
    await dest.close()


@respx.mock
async def test_ensure_folder_survives_a_concurrent_create(dest):
    """Two parallel file tasks in one new folder must not fight over it."""
    lookup = respx.get(url__regex=rf"{GRAPH}/me/drive/items/f1:/.*")
    lookup.side_effect = [
        httpx.Response(404, json={}),  # not there when we check
        httpx.Response(200, json={"id": "won", "name": "Photos", "folder": {}}),
    ]
    respx.post(f"{GRAPH}/me/drive/items/f1/children").mock(
        return_value=httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
    )
    folder = await dest.ensure_folder("f1", "Photos")
    assert folder.id == "won"
    await dest.close()


# ------------------------------------------------------------ upload sessions


@respx.mock
async def test_create_upload_session_declares_name_and_size(dest):
    route = respx.post(url__regex=rf"{GRAPH}/me/drive/items/f1:/.*:/createUploadSession").mock(
        return_value=httpx.Response(200, json={"uploadUrl": SESSION_URL})
    )
    session = await dest.create_upload_session("f1", "big.bin", size=5_000_000,
                                               conflict="replace")
    assert session.url == SESSION_URL
    body = json.loads(route.calls[0].request.content)
    # Declaring the size lets Graph refuse an over-quota upload up front.
    assert body["fileSize"] == 5_000_000
    assert body["item"]["name"] == "big.bin"
    assert body["item"]["@microsoft.graph.conflictBehavior"] == "replace"
    await dest.close()


@respx.mock
async def test_missing_upload_url_is_an_error(dest):
    respx.post(url__regex=rf"{GRAPH}/me/drive/items/f1:/.*").mock(
        return_value=httpx.Response(200, json={})
    )
    with pytest.raises(ProviderError, match="no uploadUrl"):
        await dest.create_upload_session("f1", "x.bin", size=1)
    await dest.close()


@respx.mock
async def test_chunk_sends_content_range_and_no_authorization(dest):
    """The session URL is pre-authenticated; a bearer token must not be sent."""
    total = ONEDRIVE_CHUNK_MULTIPLE * 2
    route = respx.put(SESSION_URL).mock(
        return_value=httpx.Response(202, json={"nextExpectedRanges": ["327680-"]})
    )
    data = b"x" * ONEDRIVE_CHUNK_MULTIPLE
    result = await dest.put_chunk(SESSION_URL, data, 0, total)

    assert result is None  # not the final chunk
    request = route.calls[0].request
    assert request.headers["Content-Range"] == f"bytes 0-{ONEDRIVE_CHUNK_MULTIPLE - 1}/{total}"
    assert request.headers["Content-Length"] == str(len(data))
    assert "Authorization" not in request.headers
    await dest.close()


@respx.mock
async def test_final_chunk_returns_the_finished_item(dest):
    total = ONEDRIVE_CHUNK_MULTIPLE + 100
    respx.put(SESSION_URL).mock(
        return_value=httpx.Response(201, json=_file_payload("done.bin", size=total))
    )
    item = await dest.put_chunk(SESSION_URL, b"y" * 100, ONEDRIVE_CHUNK_MULTIPLE, total)
    assert item is not None
    assert item.name == "done.bin"
    assert item.size == total
    await dest.close()


async def test_illegal_chunk_size_is_refused_before_the_request(dest):
    """Graph only accepts 320 KiB multiples; catch it locally, not after a 400."""
    with pytest.raises(ProviderError, match="not a multiple"):
        await dest.put_chunk(SESSION_URL, b"z" * 1000, 0, 10_000_000)
    await dest.close()


@respx.mock
async def test_a_short_last_chunk_is_allowed(dest):
    """Only non-final chunks are size-constrained."""
    total = 1000
    respx.put(SESSION_URL).mock(
        return_value=httpx.Response(201, json=_file_payload("small.bin", size=total))
    )
    item = await dest.put_chunk(SESSION_URL, b"q" * total, 0, total)
    assert item is not None
    await dest.close()


async def test_empty_chunk_is_refused(dest):
    with pytest.raises(ProviderError, match="empty chunk"):
        await dest.put_chunk(SESSION_URL, b"", 0, 100)
    await dest.close()


@respx.mock
async def test_session_offset_is_read_from_next_expected_ranges(dest):
    respx.get(SESSION_URL).mock(
        return_value=httpx.Response(
            200, json={"nextExpectedRanges": ["26214400-", "99999999-"]}
        )
    )
    assert await dest.session_next_offset(SESSION_URL) == 26_214_400
    await dest.close()


@respx.mock
async def test_empty_ranges_mean_the_upload_is_finished(dest):
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json={"nextExpectedRanges": []}))
    assert await dest.session_next_offset(SESSION_URL) is None
    await dest.close()


@respx.mock
async def test_a_vanished_session_reports_none(dest):
    respx.get(SESSION_URL).mock(return_value=httpx.Response(404, json={}))
    assert await dest.session_next_offset(SESSION_URL) is None
    await dest.close()


@respx.mock
async def test_a_gone_session_reports_none(dest):
    respx.get(SESSION_URL).mock(return_value=httpx.Response(410, json={}))
    assert await dest.session_next_offset(SESSION_URL) is None
    await dest.close()


@respx.mock
async def test_unexpected_session_error_is_raised(dest):
    respx.get(SESSION_URL).mock(return_value=httpx.Response(400, json={}))
    with pytest.raises(ProviderError):
        await dest.session_next_offset(SESSION_URL)
    await dest.close()


@respx.mock
async def test_cancel_session_ignores_failures(dest):
    """Cancelling is tidy-up; a failure must not derail the caller."""
    respx.delete(SESSION_URL).mock(return_value=httpx.Response(404, json={}))
    await dest.cancel_session(SESSION_URL)  # must not raise
    await dest.close()


# ---------------------------------------------------------------- simple path


@respx.mock
async def test_simple_upload_puts_the_whole_body(dest):
    route = respx.put(url__regex=rf"{GRAPH}/me/drive/items/f1:/.*:/content.*").mock(
        return_value=httpx.Response(201, json=_file_payload("tiny.txt", size=5))
    )
    item = await dest.simple_upload("f1", "tiny.txt", b"hello", conflict="replace")
    assert item.name == "tiny.txt"
    request = route.calls[0].request
    assert request.content == b"hello"
    assert request.headers["Authorization"] == "Bearer ms-access-token"
    assert "conflictBehavior=replace" in str(request.url)
    await dest.close()


@respx.mock
async def test_quota_is_reported(dest):
    respx.get(f"{GRAPH}/me/drive").mock(
        return_value=httpx.Response(
            200, json={"quota": {"remaining": 5_000_000_000, "total": 1_000_000_000_000}}
        )
    )
    remaining, total = await dest.get_quota()
    assert remaining == 5_000_000_000
    assert total == 1_000_000_000_000
    await dest.close()


@respx.mock
async def test_missing_item_raises_not_found(dest):
    respx.get(url__regex=rf"{GRAPH}/me/drive/items/nope.*").mock(
        return_value=httpx.Response(404, json={"error": {"code": "itemNotFound"}})
    )
    with pytest.raises(NotFoundError):
        await dest.get_item("nope")
    await dest.close()

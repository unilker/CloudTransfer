"""Retry policy: what gets retried, how long we wait, and when we give up."""

from __future__ import annotations

from datetime import UTC

import httpx
import pytest
import respx

from cloudtransfer.errors import NotFoundError, ProviderError
from cloudtransfer.http import (
    RETRY_STATUSES,
    backoff_delay,
    build_client,
    parse_retry_after,
    request_with_retry,
)

URL = "https://api.example.invalid/thing"


# ------------------------------------------------------------- Retry-After


def test_retry_after_seconds():
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after("0") == 0.0


def test_retry_after_http_date_becomes_a_delay():
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    when = datetime.now(UTC) + timedelta(seconds=45)
    delay = parse_retry_after(format_datetime(when))
    assert delay is not None
    assert 40 <= delay <= 50


def test_a_past_date_means_no_wait():
    assert parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0.0


@pytest.mark.parametrize("value", [None, "", "not a date", "soon"])
def test_unparseable_retry_after_is_ignored(value):
    assert parse_retry_after(value) is None


# ----------------------------------------------------------------- backoff


def test_backoff_grows_and_stays_jittered():
    first = [backoff_delay(1) for _ in range(40)]
    later = [backoff_delay(5) for _ in range(40)]
    assert all(0 < d <= 1.0 for d in first)
    assert max(later) > max(first)
    assert len(set(first)) > 1  # jitter, not a fixed schedule


def test_provider_hint_overrides_our_own_backoff():
    assert backoff_delay(1, retry_after=12.0) == 12.0


def test_backoff_is_capped():
    assert backoff_delay(50) <= 120.0
    assert backoff_delay(1, retry_after=99999.0) == 120.0


# ------------------------------------------------------------------ retrying


@respx.mock
async def test_a_throttled_request_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("cloudtransfer.http.backoff_delay", lambda *a, **k: 0.0)
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(503),
        httpx.Response(200, json={"ok": True}),
    ]
    async with build_client() as client:
        response = await request_with_retry(client, "GET", URL, max_retries=5)
    assert response.json() == {"ok": True}
    assert route.call_count == 3


@respx.mock
async def test_retries_are_eventually_exhausted(monkeypatch):
    monkeypatch.setattr("cloudtransfer.http.backoff_delay", lambda *a, **k: 0.0)
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    async with build_client() as client:
        with pytest.raises(ProviderError) as info:
            await request_with_retry(client, "GET", URL, max_retries=2)
    assert info.value.status == 503
    assert info.value.retryable is True
    assert route.call_count == 3  # the first attempt plus two retries


@respx.mock
async def test_client_errors_are_not_retried():
    """A 400 will not become a 200; retrying only burns quota."""
    route = respx.get(URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    async with build_client() as client:
        with pytest.raises(ProviderError) as info:
            await request_with_retry(client, "GET", URL, max_retries=5)
    assert route.call_count == 1
    assert info.value.retryable is False


@respx.mock
async def test_404_is_its_own_exception_type():
    respx.get(URL).mock(return_value=httpx.Response(404))
    async with build_client() as client:
        with pytest.raises(NotFoundError):
            await request_with_retry(client, "GET", URL, max_retries=1)


@respx.mock
async def test_transport_errors_are_retried(monkeypatch):
    monkeypatch.setattr("cloudtransfer.http.backoff_delay", lambda *a, **k: 0.0)
    route = respx.get(URL)
    route.side_effect = [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("too slow"),
        httpx.Response(200, json={"recovered": True}),
    ]
    async with build_client() as client:
        response = await request_with_retry(client, "GET", URL, max_retries=4)
    assert response.json() == {"recovered": True}
    assert route.call_count == 3


@respx.mock
async def test_a_persistent_transport_error_surfaces(monkeypatch):
    monkeypatch.setattr("cloudtransfer.http.backoff_delay", lambda *a, **k: 0.0)
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    async with build_client() as client:
        with pytest.raises(ProviderError, match="after 1 retries"):
            await request_with_retry(client, "GET", URL, max_retries=1)


@respx.mock
async def test_the_error_body_is_kept_for_diagnosis():
    """Drive signals special cases in the body, so it must not be discarded."""
    respx.get(URL).mock(
        return_value=httpx.Response(
            403, json={"error": {"errors": [{"reason": "cannotDownloadAbusiveFile"}]}}
        )
    )
    async with build_client() as client:
        with pytest.raises(ProviderError) as info:
            await request_with_retry(client, "GET", URL, max_retries=1)
    assert "cannotDownloadAbusiveFile" in info.value.body


def test_the_retry_set_covers_throttling_and_server_faults():
    assert 429 in RETRY_STATUSES
    assert 503 in RETRY_STATUSES
    assert 500 in RETRY_STATUSES
    assert 400 not in RETRY_STATUSES
    assert 404 not in RETRY_STATUSES

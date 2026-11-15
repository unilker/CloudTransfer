"""Shared async HTTP plumbing: one client, one retry policy.

Both Google and Microsoft throttle aggressively and both answer with a
``Retry-After`` hint when they do. Honouring that hint instead of guessing is
the difference between a transfer that finishes and one that gets rate-limited
into failure, so every provider call goes through :func:`request_with_retry`.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from typing import Any

import httpx

from .errors import NotFoundError, ProviderError

log = logging.getLogger(__name__)

# Statuses worth retrying. 429 is throttling; 5xx is the provider wobbling.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 509})

# Transport-level failures that say nothing about our request's validity.
RETRY_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0)

_MAX_BACKOFF = 120.0


def build_client(**kw: Any) -> httpx.AsyncClient:
    """Create a client tuned for long-lived chunked uploads."""
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    kw.setdefault("follow_redirects", True)
    kw.setdefault(
        "limits",
        httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=60.0),
    )
    kw.setdefault("headers", {"User-Agent": "CloudTransfer/1.0 (+streaming-relay)"})
    return httpx.AsyncClient(**kw)


def parse_retry_after(value: str | None) -> float | None:
    """Parse ``Retry-After``, which may be seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def backoff_delay(attempt: int, retry_after: float | None = None, *, base: float = 1.0) -> float:
    """Exponential backoff with jitter, overridden by the provider's hint."""
    if retry_after is not None:
        return min(retry_after, _MAX_BACKOFF)
    delay = min(base * (2 ** max(attempt - 1, 0)), _MAX_BACKOFF)
    return delay * (0.5 + random.random() / 2.0)  # noqa: S311 - jitter, not crypto


async def _body_preview(response: httpx.Response, limit: int = 600) -> str:
    try:
        if not response.is_closed:
            await response.aread()
        text = response.text
    except Exception:  # pragma: no cover - diagnostics only
        return "<unreadable body>"
    return text[:limit]


def classify(response: httpx.Response, body: str = "") -> ProviderError:
    """Turn a failed response into the right exception type."""
    status = response.status_code
    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    message = f"{response.request.method} {response.request.url} failed"
    if status == 404:
        return NotFoundError(message, status=status, body=body)
    return ProviderError(
        message,
        status=status,
        retryable=status in RETRY_STATUSES,
        retry_after=retry_after,
        body=body,
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 6,
    retry_on: frozenset[int] = RETRY_STATUSES,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a request, retrying throttling and transient transport failures.

    Raises :class:`ProviderError` once retries are exhausted or the failure is
    not retryable. Successful responses are returned fully read.
    """
    attempt = 0
    last_error: Exception | None = None

    while attempt <= max_retries:
        attempt += 1
        try:
            response = await client.request(method, url, **kwargs)
        except RETRY_EXCEPTIONS as exc:
            last_error = exc
            if attempt > max_retries:
                break
            delay = backoff_delay(attempt)
            log.warning(
                "%s %s: %s (transport); retry %d/%d in %.1fs",
                method, url, type(exc).__name__, attempt, max_retries, delay,
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code < 400:
            return response

        body = await _body_preview(response)
        error = classify(response, body)

        if response.status_code not in retry_on or attempt > max_retries:
            raise error

        delay = backoff_delay(attempt, error.retry_after)
        log.warning(
            "%s %s: HTTP %d; retry %d/%d in %.1fs",
            method, url, response.status_code, attempt, max_retries, delay,
        )
        await asyncio.sleep(delay)
        last_error = error

    if isinstance(last_error, ProviderError):
        raise last_error
    raise ProviderError(
        f"{method} {url} failed after {max_retries} retries: {last_error}",
        retryable=True,
    ) from last_error

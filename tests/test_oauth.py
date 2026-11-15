"""The OAuth flow itself: PKCE, public clients, and stored applications."""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
import respx

from cloudtransfer.appconfig import (
    clear_oauth_app,
    load_oauth_app,
    require_oauth_app,
    save_oauth_app,
    setup_status,
)
from cloudtransfer.errors import AuthError, ConfigError
from cloudtransfer.oauth import (
    OAuthApp,
    authorize_url,
    exchange_code,
    new_pkce_verifier,
    new_state,
    pkce_challenge,
    refresh_tokens,
)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

REDIRECT = "http://localhost:8765/auth/google/callback"


def _google(secret: str = "gsecret") -> OAuthApp:
    return OAuthApp(provider="google", client_id="gid", client_secret=secret)


def _microsoft(secret: str = "") -> OAuthApp:
    return OAuthApp(provider="microsoft", client_id="mid", client_secret=secret)


# ------------------------------------------------------------------- PKCE


def test_verifier_is_within_the_rfc_length_range():
    for _ in range(20):
        verifier = new_pkce_verifier()
        assert 43 <= len(verifier) <= 128


def test_verifiers_are_not_reused():
    assert len({new_pkce_verifier() for _ in range(50)}) == 50
    assert len({new_state() for _ in range(50)}) == 50


def test_challenge_is_unpadded_base64url_sha256():
    verifier = "a-test-verifier-value-that-is-long-enough-to-be-valid"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    challenge = pkce_challenge(verifier)
    assert challenge == expected
    assert "=" not in challenge  # padding would be rejected
    assert "+" not in challenge and "/" not in challenge  # must be URL-safe


# -------------------------------------------------------- authorization URL


def test_authorize_url_carries_pkce_and_scopes():
    verifier = new_pkce_verifier()
    url = httpx.URL(authorize_url(_google(), REDIRECT, "state123", verifier))
    params = url.params

    assert url.host == "accounts.google.com"
    assert params["client_id"] == "gid"
    assert params["redirect_uri"] == REDIRECT
    assert params["state"] == "state123"
    assert params["code_challenge"] == pkce_challenge(verifier)
    assert params["code_challenge_method"] == "S256"
    assert "drive.readonly" in params["scope"]
    # Without these two Google issues no refresh token.
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    # The verifier itself must never leave this process.
    assert verifier not in str(url)


def test_microsoft_authorize_url_uses_the_configured_tenant():
    app = OAuthApp(provider="microsoft", client_id="mid", tenant="consumers")
    url = httpx.URL(authorize_url(app, REDIRECT, "s", new_pkce_verifier()))
    assert "/consumers/oauth2/v2.0/authorize" in url.path
    assert "offline_access" in url.params["scope"]


def test_an_empty_tenant_falls_back_to_common():
    app = OAuthApp(provider="microsoft", client_id="mid", tenant="")
    url = httpx.URL(authorize_url(app, REDIRECT, "s", new_pkce_verifier()))
    assert "/common/" in url.path


def test_unknown_provider_is_refused():
    with pytest.raises(AuthError, match="Unknown provider"):
        authorize_url(OAuthApp(provider="dropbox", client_id="x"), REDIRECT, "s", "v")


# ---------------------------------------------------------- token exchange


@respx.mock
async def test_exchange_sends_the_verifier_and_the_secret():
    route = respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        )
    )
    tokens = await exchange_code(_google(), REDIRECT, "the-code", "the-verifier")

    assert tokens.access_token == "at"
    assert tokens.refresh_token == "rt"
    body = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert body["code_verifier"] == "the-verifier"
    assert body["client_secret"] == "gsecret"
    assert body["grant_type"] == "authorization_code"
    assert body["redirect_uri"] == REDIRECT


@respx.mock
async def test_a_public_client_sends_no_secret():
    """A desktop registration has none, and sending an empty one is an error."""
    route = respx.post(MS_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )
    )
    app = _microsoft()
    assert app.is_public_client is True

    await exchange_code(app, REDIRECT, "code", "verifier")

    body = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert "client_secret" not in body
    assert body["client_id"] == "mid"
    assert body["code_verifier"] == "verifier"


@respx.mock
async def test_a_response_without_a_refresh_token_is_an_error():
    """Silently accepting it would leave the app dead in an hour."""
    respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    )
    with pytest.raises(AuthError, match="no refresh token"):
        await exchange_code(_google(), REDIRECT, "code", "verifier")


@respx.mock
async def test_refresh_keeps_the_old_token_when_none_is_returned():
    """Google omits refresh_token on refresh; dropping it would break the next one."""
    respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new-at", "expires_in": 3600})
    )
    tokens = await refresh_tokens(_google(), "original-rt")
    assert tokens.access_token == "new-at"
    assert tokens.refresh_token == "original-rt"


@respx.mock
async def test_microsoft_refresh_sends_the_scope_list():
    route = respx.post(MS_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rotated", "expires_in": 3600}
        )
    )
    tokens = await refresh_tokens(_microsoft(), "old-rt")
    assert tokens.refresh_token == "rotated"  # Microsoft rotates it
    body = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert "offline_access" in body["scope"]


@respx.mock
async def test_a_rejected_refresh_asks_the_user_to_reconnect():
    respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(AuthError, match="reconnect the account"):
        await refresh_tokens(_google(), "revoked")


# ------------------------------------------------- stored application setup


async def test_nothing_is_configured_on_a_fresh_install(db):
    assert await load_oauth_app("google", db) is None
    assert await load_oauth_app("microsoft", db) is None

    status = await setup_status(db)
    assert status["google"]["configured"] is False
    assert status["microsoft"]["configured"] is False

    with pytest.raises(ConfigError, match="has not been set up"):
        await require_oauth_app("google", db)


async def test_saving_and_reloading_an_application(db):
    await save_oauth_app(
        "google", client_id="  my-client-id  ", client_secret=" my-secret ", settings=db
    )
    app = await load_oauth_app("google", db)

    assert app is not None
    assert app.client_id == "my-client-id"  # whitespace trimmed
    assert app.client_secret == "my-secret"
    assert app.source == "stored"
    assert app.is_public_client is False


async def test_a_desktop_registration_needs_no_secret(db):
    await save_oauth_app("microsoft", client_id="mid", client_secret="", settings=db)
    app = await load_oauth_app("microsoft", db)
    assert app.is_public_client is True
    assert (await setup_status(db))["microsoft"]["has_secret"] is False


async def test_the_secret_is_not_stored_in_the_clear(db):
    from sqlalchemy import select

    from cloudtransfer.db import session_scope
    from cloudtransfer.models import OAuthAppRow

    await save_oauth_app("google", client_id="gid", client_secret="TOP-SECRET", settings=db)
    async with session_scope(db) as session:
        row = (await session.execute(select(OAuthAppRow))).scalar_one()
    assert "TOP-SECRET" not in row.client_secret_enc
    assert row.client_secret_enc


async def test_saving_twice_replaces_rather_than_duplicates(db):
    await save_oauth_app("google", client_id="first", settings=db)
    await save_oauth_app("google", client_id="second", settings=db)
    app = await load_oauth_app("google", db)
    assert app.client_id == "second"


async def test_a_client_id_is_required(db):
    with pytest.raises(ConfigError, match="client ID is required"):
        await save_oauth_app("google", client_id="   ", settings=db)


async def test_an_unknown_provider_is_refused(db):
    with pytest.raises(ConfigError, match="Unknown provider"):
        await save_oauth_app("dropbox", client_id="x", settings=db)
    with pytest.raises(ConfigError, match="Unknown provider"):
        await load_oauth_app("dropbox", db)


async def test_clearing_falls_back_to_the_environment(db, monkeypatch):
    await save_oauth_app("google", client_id="stored-id", settings=db)
    assert (await load_oauth_app("google", db)).client_id == "stored-id"

    # The environment is the fallback a server deployment relies on.
    db.google_client_id = "env-id"
    db.google_client_secret = "env-secret"
    assert (await load_oauth_app("google", db)).client_id == "stored-id"

    assert await clear_oauth_app("google", db) is True
    app = await load_oauth_app("google", db)
    assert app.client_id == "env-id"
    assert app.source == "env"

    # Clearing again is harmless.
    assert await clear_oauth_app("google", db) is False


async def test_stored_settings_win_over_the_environment(db):
    """A value changed in the interface has to actually take effect."""
    db.ms_client_id = "env-id"
    db.ms_tenant = "consumers"
    await save_oauth_app("microsoft", client_id="stored-id", tenant="common", settings=db)

    app = await load_oauth_app("microsoft", db)
    assert app.client_id == "stored-id"
    assert app.tenant == "common"

    status = await setup_status(db)
    assert status["microsoft"]["source"] == "stored"
    assert status["microsoft"]["editable"] is True


async def test_environment_backed_settings_are_marked_read_only(db):
    db.google_client_id = "env-id"
    db.google_client_secret = "env-secret"
    status = await setup_status(db)
    assert status["google"]["configured"] is True
    assert status["google"]["source"] == "env"
    assert status["google"]["editable"] is False

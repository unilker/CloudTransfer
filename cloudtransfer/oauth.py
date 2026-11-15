"""OAuth 2.0 authorization-code flow for Google and Microsoft.

Both providers use the same shape, so one implementation covers them with a
small per-provider descriptor. Two details matter in practice:

* Google only issues a refresh token when asked with ``access_type=offline``,
  and only re-issues one on ``prompt=consent``. Without it the app works until
  the first access token expires an hour later and then silently stops.
* Microsoft requires ``offline_access`` in the scope list for the same reason,
  and rotates the refresh token on every exchange, so the new one must be
  stored each time.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .errors import AuthError
from .http import build_client, request_with_retry

# Read-only on the source: this app never needs to modify Google Drive.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "openid",
    "email",
]

# Read/write on the destination, plus offline_access for the refresh token.
MS_SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/Files.ReadWrite",
    "https://graph.microsoft.com/User.Read",
]


@dataclass(slots=True)
class ProviderConfig:
    name: str
    authorize_url: str
    token_url: str
    scopes: list[str]
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


def google_config() -> ProviderConfig:
    return ProviderConfig(
        name="google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=GOOGLE_SCOPES,
        extra_authorize_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
    )


def microsoft_config(tenant: str) -> ProviderConfig:
    return ProviderConfig(
        name="microsoft",
        authorize_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        scopes=MS_SCOPES,
        extra_authorize_params={"prompt": "select_account"},
    )


def config_for(provider: str, settings: Settings) -> ProviderConfig:
    if provider == "google":
        return google_config()
    if provider == "microsoft":
        return microsoft_config(settings.ms_tenant)
    raise AuthError(f"Unknown provider {provider!r}")


def credentials_for(provider: str, settings: Settings) -> tuple[str, str, str]:
    """Return (client_id, client_secret, redirect_uri) for a provider."""
    settings.require_provider(provider)
    if provider == "google":
        return (
            settings.google_client_id,
            settings.google_client_secret,
            settings.google_redirect_uri,
        )
    return settings.ms_client_id, settings.ms_client_secret, settings.ms_redirect_uri


@dataclass(slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float
    scope: str = ""

    @property
    def expired(self) -> bool:
        # Refresh a minute early so a long upload never starts on a dying token.
        return time.time() >= self.expires_at - 60

    @classmethod
    def from_response(
        cls, payload: dict[str, Any], previous_refresh: str | None = None
    ) -> TokenSet:
        expires_in = float(payload.get("expires_in", 3600))
        return cls(
            access_token=payload["access_token"],
            # Google omits refresh_token on refresh; Microsoft rotates it.
            refresh_token=payload.get("refresh_token") or previous_refresh,
            expires_at=time.time() + expires_in,
            scope=payload.get("scope", ""),
        )


def new_state() -> str:
    """CSRF state for the authorization redirect."""
    return secrets.token_urlsafe(24)


def authorize_url(provider: str, settings: Settings, state: str) -> str:
    cfg = config_for(provider, settings)
    client_id, _, redirect_uri = credentials_for(provider, settings)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(cfg.scopes),
        "state": state,
        **cfg.extra_authorize_params,
    }
    return str(httpx.URL(cfg.authorize_url, params=params))


async def exchange_code(provider: str, settings: Settings, code: str) -> TokenSet:
    cfg = config_for(provider, settings)
    client_id, client_secret, redirect_uri = credentials_for(provider, settings)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    async with build_client() as client:
        response = await request_with_retry(
            client, "POST", cfg.token_url, data=data, max_retries=3
        )
    payload = response.json()
    tokens = TokenSet.from_response(payload)
    if not tokens.refresh_token:
        raise AuthError(
            f"{provider} returned no refresh token. Revoke this app's access in the "
            f"provider's account settings and connect again so consent is re-prompted."
        )
    return tokens


async def refresh_tokens(provider: str, settings: Settings, refresh_token: str) -> TokenSet:
    cfg = config_for(provider, settings)
    client_id, client_secret, _ = credentials_for(provider, settings)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if provider == "microsoft":
        data["scope"] = " ".join(cfg.scopes)
    async with build_client() as client:
        try:
            response = await request_with_retry(
                client, "POST", cfg.token_url, data=data, max_retries=3
            )
        except Exception as exc:
            raise AuthError(
                f"Could not refresh the {provider} token; reconnect the account. ({exc})"
            ) from exc
    return TokenSet.from_response(response.json(), previous_refresh=refresh_token)


async def fetch_identity(provider: str, access_token: str) -> dict[str, str]:
    """Best-effort display name/email so the UI can label the connection."""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = (
        "https://www.googleapis.com/drive/v3/about?fields=user(displayName,emailAddress)"
        if provider == "google"
        else "https://graph.microsoft.com/v1.0/me?$select=displayName,userPrincipalName,mail"
    )
    try:
        async with build_client() as client:
            response = await request_with_retry(client, "GET", url, headers=headers, max_retries=2)
        payload = response.json()
    except Exception:
        return {"display_name": "", "email": ""}

    if provider == "google":
        user = payload.get("user", {}) or {}
        return {
            "display_name": user.get("displayName", ""),
            "email": user.get("emailAddress", ""),
        }
    return {
        "display_name": payload.get("displayName", ""),
        "email": payload.get("mail") or payload.get("userPrincipalName", ""),
    }

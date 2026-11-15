"""OAuth 2.0 authorization-code flow, for both a hosted app and a desktop one.

Both providers use the same shape, so one implementation covers them with a
small per-provider descriptor. The details that matter in practice:

* Google only issues a refresh token when asked with ``access_type=offline``,
  and only re-issues one on ``prompt=consent``. Without it the app works until
  the first access token expires an hour later and then silently stops.
* Microsoft requires ``offline_access`` in the scope list for the same reason,
  and rotates the refresh token on every exchange, so the new one must be
  stored each time.
* **PKCE is always used.** A desktop build is a public client: its client ID
  ships inside the installer, and Microsoft's "Mobile and desktop" app type
  issues no secret at all. PKCE is what stops an intercepted authorization
  code from being redeemed by anyone else, and both providers accept it on
  confidential clients too, so there is no reason to branch.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .errors import AuthError
from .http import build_client, request_with_retry

# Full Drive access, because files travel both ways. Read-only would be enough
# to send files to OneDrive, but not to receive them: creating a file inside a
# folder the user already has requires write access to Drive, and the narrower
# "drive.file" scope only reaches files this app itself created.
GOOGLE_WRITE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

GOOGLE_SCOPES = [
    GOOGLE_WRITE_SCOPE,
    "openid",
    "email",
]

# Read/write on the destination, plus offline_access for the refresh token.
MS_SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/Files.ReadWrite",
    "https://graph.microsoft.com/User.Read",
]

PROVIDERS = ("google", "microsoft")


def google_can_write(granted_scope: str | None) -> bool:
    """Whether a stored Google grant allows writing to Drive.

    An account connected before transfers ran both ways holds only the
    read-only scope. Nothing refreshes that: the user has to reconnect, and
    saying so beats letting the first upload fail with a bare 403.
    """
    return GOOGLE_WRITE_SCOPE in (granted_scope or "").split()

HUMAN_NAMES = {"google": "Google Drive", "microsoft": "OneDrive"}


@dataclass(slots=True)
class OAuthApp:
    """The registered application a user connects their account through.

    ``client_secret`` is empty for a public (desktop) client. Where it is set,
    it is sent on the token exchange as a confidential client would.
    """

    provider: str
    client_id: str
    client_secret: str = ""
    tenant: str = "common"
    source: str = "stored"  # "stored" (set up in the app) or "env"

    @property
    def is_public_client(self) -> bool:
        return not self.client_secret


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
    tenant = tenant or "common"
    return ProviderConfig(
        name="microsoft",
        authorize_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        scopes=MS_SCOPES,
        extra_authorize_params={"prompt": "select_account"},
    )


def config_for(app: OAuthApp) -> ProviderConfig:
    if app.provider == "google":
        return google_config()
    if app.provider == "microsoft":
        return microsoft_config(app.tenant)
    raise AuthError(f"Unknown provider {app.provider!r}")


# --------------------------------------------------------------------- PKCE


def new_state() -> str:
    """CSRF state for the authorization redirect."""
    return secrets.token_urlsafe(24)


def new_pkce_verifier() -> str:
    """A high-entropy code verifier, within RFC 7636's 43-128 character range."""
    return secrets.token_urlsafe(64)[:128]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ------------------------------------------------------------------- tokens


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


def authorize_url(app: OAuthApp, redirect_uri: str, state: str, code_verifier: str) -> str:
    cfg = config_for(app)
    params = {
        "client_id": app.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(cfg.scopes),
        "state": state,
        "code_challenge": pkce_challenge(code_verifier),
        "code_challenge_method": "S256",
        **cfg.extra_authorize_params,
    }
    return str(httpx.URL(cfg.authorize_url, params=params))


def _token_request_data(app: OAuthApp) -> dict[str, str]:
    data = {"client_id": app.client_id}
    if app.client_secret:
        data["client_secret"] = app.client_secret
    return data


async def exchange_code(
    app: OAuthApp, redirect_uri: str, code: str, code_verifier: str
) -> TokenSet:
    cfg = config_for(app)
    data = {
        **_token_request_data(app),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    async with build_client() as client:
        response = await request_with_retry(
            client, "POST", cfg.token_url, data=data, max_retries=3
        )
    tokens = TokenSet.from_response(response.json())
    if not tokens.refresh_token:
        raise AuthError(
            f"{HUMAN_NAMES.get(app.provider, app.provider)} returned no refresh token. "
            f"Remove this app's access in your account settings and connect again, so "
            f"the consent screen is shown afresh."
        )
    return tokens


async def refresh_tokens(app: OAuthApp, refresh_token: str) -> TokenSet:
    cfg = config_for(app)
    data = {
        **_token_request_data(app),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if app.provider == "microsoft":
        data["scope"] = " ".join(cfg.scopes)
    async with build_client() as client:
        try:
            response = await request_with_retry(
                client, "POST", cfg.token_url, data=data, max_retries=3
            )
        except Exception as exc:
            raise AuthError(
                f"Could not refresh the {HUMAN_NAMES.get(app.provider, app.provider)} "
                f"token; reconnect the account. ({exc})"
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

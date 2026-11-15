"""Where the registered OAuth applications live.

A server deployment can set ``CT_GOOGLE_CLIENT_ID`` and friends in the
environment. The packaged desktop build cannot: there is no shell to export
variables in and no file the user should have to edit. So the same values can
also be stored in the database through a first-run wizard, and that is what the
installer relies on.

Stored values take precedence over environment variables, so a setting changed
in the interface actually takes effect.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from .config import Settings, get_settings
from .crypto import decrypt, encrypt
from .db import session_scope
from .errors import ConfigError
from .models import OAuthAppRow
from .oauth import HUMAN_NAMES, PROVIDERS, OAuthApp

log = logging.getLogger(__name__)


def _from_env(provider: str, settings: Settings) -> OAuthApp | None:
    if provider == "google" and settings.google_client_id:
        return OAuthApp(
            provider="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            source="env",
        )
    if provider == "microsoft" and settings.ms_client_id:
        return OAuthApp(
            provider="microsoft",
            client_id=settings.ms_client_id,
            client_secret=settings.ms_client_secret,
            tenant=settings.ms_tenant,
            source="env",
        )
    return None


async def load_oauth_app(provider: str, settings: Settings | None = None) -> OAuthApp | None:
    """Return the configured application for a provider, or ``None``."""
    if provider not in PROVIDERS:
        raise ConfigError(f"Unknown provider {provider!r}")
    settings = settings or get_settings()

    async with session_scope(settings) as session:
        row = (
            await session.execute(select(OAuthAppRow).where(OAuthAppRow.provider == provider))
        ).scalar_one_or_none()
        if row is not None:
            secret = ""
            if row.client_secret_enc:
                secret = decrypt(settings.resolve_secret_key(), row.client_secret_enc)
            return OAuthApp(
                provider=provider,
                client_id=row.client_id,
                client_secret=secret,
                tenant=row.tenant or "common",
                source="stored",
            )

    return _from_env(provider, settings)


async def require_oauth_app(provider: str, settings: Settings | None = None) -> OAuthApp:
    app = await load_oauth_app(provider, settings)
    if app is None:
        human = HUMAN_NAMES.get(provider, provider)
        raise ConfigError(
            f"{human} has not been set up yet. Open the setup step on the home "
            f"page and paste the client ID you created."
        )
    return app


async def save_oauth_app(
    provider: str,
    *,
    client_id: str,
    client_secret: str = "",
    tenant: str = "common",
    settings: Settings | None = None,
) -> OAuthApp:
    """Store (or replace) the registered application for a provider."""
    if provider not in PROVIDERS:
        raise ConfigError(f"Unknown provider {provider!r}")
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    tenant = (tenant or "common").strip() or "common"
    if not client_id:
        raise ConfigError("A client ID is required.")

    settings = settings or get_settings()
    secret_key = settings.resolve_secret_key()

    async with session_scope(settings) as session:
        row = (
            await session.execute(select(OAuthAppRow).where(OAuthAppRow.provider == provider))
        ).scalar_one_or_none()
        if row is None:
            row = OAuthAppRow(provider=provider)
            session.add(row)
        row.client_id = client_id
        row.client_secret_enc = encrypt(secret_key, client_secret) if client_secret else ""
        row.tenant = tenant

    log.info("saved %s application settings", provider)
    return OAuthApp(
        provider=provider,
        client_id=client_id,
        client_secret=client_secret,
        tenant=tenant,
        source="stored",
    )


async def clear_oauth_app(provider: str, settings: Settings | None = None) -> bool:
    """Forget stored settings for a provider, falling back to the environment."""
    settings = settings or get_settings()
    async with session_scope(settings) as session:
        row = (
            await session.execute(select(OAuthAppRow).where(OAuthAppRow.provider == provider))
        ).scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
    return True


async def setup_status(settings: Settings | None = None) -> dict:
    """What the first-run wizard needs to render itself."""
    settings = settings or get_settings()
    out: dict[str, dict] = {}
    for provider in PROVIDERS:
        app = await load_oauth_app(provider, settings)
        out[provider] = {
            "configured": app is not None,
            "client_id": app.client_id if app else "",
            "has_secret": bool(app and app.client_secret),
            "tenant": app.tenant if app else "common",
            "source": app.source if app else None,
            "editable": app is None or app.source == "stored",
        }
    return out

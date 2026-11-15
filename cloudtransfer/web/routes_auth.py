"""First-run setup, OAuth connect/disconnect, and overall status."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..accounts import disconnect_account, list_accounts, save_account
from ..appconfig import clear_oauth_app, load_oauth_app, save_oauth_app, setup_status
from ..config import Settings, get_settings
from ..errors import AuthError, ConfigError
from ..oauth import (
    HUMAN_NAMES,
    PROVIDERS,
    authorize_url,
    exchange_code,
    new_pkce_verifier,
    new_state,
)

log = logging.getLogger(__name__)
router = APIRouter()

_FLOW_KEY = "oauth_flow"


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider {provider!r}")


def public_base_url(request: Request, settings: Settings) -> str:
    """The address this instance is reached at, for building redirect URIs.

    Behind a proxy the request arrives as plain http, so an explicitly
    configured CT_BASE_URL wins. The desktop build sets no such value and may
    land on any free port, so there the request's own address is correct - and
    the setup page shows the result, so whatever it is gets registered.
    """
    if settings.base_url_is_explicit:
        return settings.base_url
    return str(request.base_url).rstrip("/")


def redirect_uri_for(request: Request, settings: Settings, provider: str) -> str:
    return f"{public_base_url(request, settings)}/auth/{provider}/callback"


# ------------------------------------------------------------------- setup


class AppCredentials(BaseModel):
    client_id: str = Field(..., min_length=1, max_length=512)
    client_secret: str = Field("", max_length=512)
    tenant: str = Field("common", max_length=64)


@router.get("/api/setup")
async def get_setup(request: Request) -> dict:
    """What the first-run wizard renders, including the exact redirect URIs."""
    settings = get_settings()
    status = await setup_status(settings)
    for provider in PROVIDERS:
        status[provider]["redirect_uri"] = redirect_uri_for(request, settings, provider)
        status[provider]["name"] = HUMAN_NAMES[provider]
    return {
        "providers": status,
        "base_url": public_base_url(request, settings),
        "complete": all(entry["configured"] for entry in status.values()),
    }


@router.post("/api/setup/{provider}")
async def post_setup(provider: str, body: AppCredentials) -> dict:
    _check_provider(provider)
    try:
        app = await save_oauth_app(
            provider,
            client_id=body.client_id,
            client_secret=body.client_secret,
            tenant=body.tenant,
        )
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "provider": provider,
        "client_id": app.client_id,
        "has_secret": bool(app.client_secret),
        "public_client": app.is_public_client,
    }


@router.delete("/api/setup/{provider}")
async def delete_setup(provider: str) -> dict:
    _check_provider(provider)
    return {"cleared": await clear_oauth_app(provider)}


# ----------------------------------------------------------------- connect


@router.get("/auth/{provider}/start")
async def start(provider: str, request: Request) -> RedirectResponse:
    _check_provider(provider)
    settings = get_settings()

    app = await load_oauth_app(provider, settings)
    if app is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{HUMAN_NAMES[provider]} is not set up yet. Finish the setup "
                f"step first, then connect."
            ),
        )

    state = new_state()
    verifier = new_pkce_verifier()
    redirect_uri = redirect_uri_for(request, settings, provider)
    request.session[_FLOW_KEY] = {
        "provider": provider,
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    return RedirectResponse(
        authorize_url(app, redirect_uri, state, verifier), status_code=307
    )


@router.get("/auth/{provider}/callback")
async def callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    _check_provider(provider)

    if error:
        detail = error_description or error
        log.warning("%s declined authorization: %s", provider, detail)
        return RedirectResponse(f"/?error={provider}+authorization+failed", status_code=303)

    flow = request.session.pop(_FLOW_KEY, None) or {}
    if (
        not code
        or not state
        or flow.get("state") != state
        or flow.get("provider") != provider
    ):
        # A mismatched state means the redirect did not originate here.
        raise HTTPException(
            status_code=400,
            detail="Authorization state did not match. Start the connection again.",
        )

    settings = get_settings()
    app = await load_oauth_app(provider, settings)
    if app is None:  # pragma: no cover - cleared mid-flow
        raise HTTPException(status_code=400, detail=f"{HUMAN_NAMES[provider]} is not set up.")

    try:
        tokens = await exchange_code(
            app,
            flow.get("redirect_uri") or redirect_uri_for(request, settings, provider),
            code,
            flow.get("verifier", ""),
        )
        await save_account(provider, tokens, settings)
    except (AuthError, ConfigError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse("/?connected=" + provider, status_code=303)


# ------------------------------------------------------------------ status


@router.get("/api/status")
async def status(request: Request) -> dict:
    settings = get_settings()
    accounts = {account.provider: account for account in await list_accounts()}
    setup = await setup_status(settings)

    def describe(provider: str) -> dict | None:
        account = accounts.get(provider)
        if account is None:
            return None
        return {
            "label": account.label,
            "display_name": account.display_name,
            "email": account.email,
            "connected_at": account.created_at.isoformat() if account.created_at else None,
        }

    return {
        "google": describe("google"),
        "microsoft": describe("microsoft"),
        "configured": {p: setup[p]["configured"] for p in PROVIDERS},
        "setup_complete": all(setup[p]["configured"] for p in PROVIDERS),
        "redirect_uris": {
            p: redirect_uri_for(request, settings, p) for p in PROVIDERS
        },
        "transfer": {
            "chunk_size": settings.chunk_size,
            "max_parallel_files": settings.max_parallel_files,
            "verify_hashes": settings.verify_hashes,
        },
    }


@router.post("/api/accounts/{provider}/disconnect")
async def disconnect(provider: str) -> dict:
    _check_provider(provider)
    removed = await disconnect_account(provider)
    return {"disconnected": removed}

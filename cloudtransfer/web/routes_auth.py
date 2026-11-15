"""OAuth connect/disconnect endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..accounts import disconnect_account, list_accounts, save_account
from ..config import get_settings
from ..errors import AuthError, ConfigError
from ..oauth import authorize_url, exchange_code, new_state

log = logging.getLogger(__name__)
router = APIRouter()

PROVIDERS = {"google", "microsoft"}
_STATE_KEY = "oauth_state"


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider {provider!r}")


@router.get("/auth/{provider}/start")
async def start(provider: str, request: Request) -> RedirectResponse:
    _check_provider(provider)
    settings = get_settings()
    try:
        state = new_state()
        request.session[_STATE_KEY] = {"provider": provider, "state": state}
        url = authorize_url(provider, settings, state)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=307)


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

    stored = request.session.pop(_STATE_KEY, None) or {}
    if not code or not state or stored.get("state") != state or stored.get("provider") != provider:
        # A mismatched state means the redirect did not originate here.
        raise HTTPException(
            status_code=400,
            detail="Authorization state did not match. Start the connection again.",
        )

    settings = get_settings()
    try:
        tokens = await exchange_code(provider, settings, code)
        await save_account(provider, tokens, settings)
    except (AuthError, ConfigError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse("/?connected=" + provider, status_code=303)


@router.get("/api/status")
async def status() -> dict:
    settings = get_settings()
    accounts = {account.provider: account for account in await list_accounts()}

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
        "configured": {
            "google": settings.google_configured,
            "microsoft": settings.ms_configured,
        },
        "redirect_uris": {
            "google": settings.google_redirect_uri,
            "microsoft": settings.ms_redirect_uri,
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

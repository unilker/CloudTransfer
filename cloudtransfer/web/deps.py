"""Shared request dependencies: settings, providers, account checks."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException

from ..accounts import TokenManager, load_account
from ..config import Settings, get_settings
from ..http import build_client
from ..providers.gdrive import GoogleDriveDest, GoogleDriveSource
from ..providers.onedrive import OneDriveDest


def settings_dep() -> Settings:
    return get_settings()


async def require_account(provider: str) -> None:
    if await load_account(provider) is None:
        human = "Google Drive" if provider == "google" else "OneDrive"
        raise HTTPException(
            status_code=409,
            detail=f"{human} is not connected yet. Connect it on the home page first.",
        )


async def drive_source() -> AsyncIterator[GoogleDriveSource]:
    await require_account("google")
    settings = get_settings()
    async with build_client() as client:
        yield GoogleDriveSource(TokenManager("google", settings).get_token, settings, client=client)


async def drive_dest() -> AsyncIterator[GoogleDriveDest]:
    await require_account("google")
    settings = get_settings()
    async with build_client() as client:
        yield GoogleDriveDest(TokenManager("google", settings).get_token, settings, client=client)


async def onedrive_dest() -> AsyncIterator[OneDriveDest]:
    await require_account("microsoft")
    settings = get_settings()
    async with build_client() as client:
        yield OneDriveDest(TokenManager("microsoft", settings).get_token, settings, client=client)

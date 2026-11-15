"""Connected-account storage and access-token lifecycle.

Providers need a valid bearer token for calls that can run for hours. The
manager refreshes on demand behind a lock, writes the rotated refresh token
back, and hands out the access token.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from .config import Settings, get_settings
from .crypto import decrypt, encrypt
from .db import session_scope
from .errors import AuthError
from .models import Account
from .oauth import TokenSet, fetch_identity, refresh_tokens

log = logging.getLogger(__name__)


async def save_account(
    provider: str, tokens: TokenSet, settings: Settings | None = None
) -> Account:
    """Insert or replace the stored account for a provider."""
    settings = settings or get_settings()
    secret = settings.resolve_secret_key()
    identity = await fetch_identity(provider, tokens.access_token)

    if not tokens.refresh_token:  # pragma: no cover - guarded upstream
        raise AuthError(f"{provider} did not return a refresh token")

    async with session_scope(settings) as session:
        existing = (
            await session.execute(select(Account).where(Account.provider == provider))
        ).scalar_one_or_none()
        if existing is None:
            existing = Account(provider=provider)
            session.add(existing)
        existing.access_token_enc = encrypt(secret, tokens.access_token)
        existing.refresh_token_enc = encrypt(secret, tokens.refresh_token)
        existing.expires_at = tokens.expires_at
        existing.scope = tokens.scope
        existing.display_name = identity.get("display_name", "")
        existing.email = identity.get("email", "")
        await session.flush()
        account_id = existing.id

    log.info("connected %s account %s", provider, identity.get("email") or "(unknown)")
    return await load_account(provider, settings) or Account(id=account_id, provider=provider)


async def load_account(provider: str, settings: Settings | None = None) -> Account | None:
    settings = settings or get_settings()
    async with session_scope(settings) as session:
        return (
            await session.execute(select(Account).where(Account.provider == provider))
        ).scalar_one_or_none()


async def list_accounts(settings: Settings | None = None) -> list[Account]:
    async with session_scope(settings or get_settings()) as session:
        return list((await session.execute(select(Account))).scalars())


async def disconnect_account(provider: str, settings: Settings | None = None) -> bool:
    async with session_scope(settings or get_settings()) as session:
        account = (
            await session.execute(select(Account).where(Account.provider == provider))
        ).scalar_one_or_none()
        if account is None:
            return False
        await session.delete(account)
        return True


class TokenManager:
    """Hands out access tokens for one provider, refreshing as needed."""

    def __init__(self, provider: str, settings: Settings | None = None) -> None:
        self.provider = provider
        self.settings = settings or get_settings()
        self._lock = asyncio.Lock()
        self._cached: TokenSet | None = None

    async def _load(self) -> TokenSet:
        account = await load_account(self.provider, self.settings)
        if account is None:
            raise AuthError(
                f"No {self.provider} account is connected. Open the app and connect it first."
            )
        secret = self.settings.resolve_secret_key()
        return TokenSet(
            access_token=decrypt(secret, account.access_token_enc),
            refresh_token=decrypt(secret, account.refresh_token_enc),
            expires_at=account.expires_at or 0.0,
            scope=account.scope or "",
        )

    async def _persist(self, tokens: TokenSet) -> None:
        secret = self.settings.resolve_secret_key()
        async with session_scope(self.settings) as session:
            account = (
                await session.execute(select(Account).where(Account.provider == self.provider))
            ).scalar_one_or_none()
            if account is None:  # pragma: no cover - disconnected mid-flight
                raise AuthError(f"{self.provider} account disappeared while refreshing")
            account.access_token_enc = encrypt(secret, tokens.access_token)
            if tokens.refresh_token:
                account.refresh_token_enc = encrypt(secret, tokens.refresh_token)
            account.expires_at = tokens.expires_at
            if tokens.scope:
                account.scope = tokens.scope

    async def get_token(self) -> str:
        async with self._lock:
            tokens = self._cached or await self._load()
            if tokens.expired:
                if not tokens.refresh_token:
                    raise AuthError(
                        f"The {self.provider} token expired and no refresh token is stored; "
                        f"reconnect the account."
                    )
                log.debug("refreshing %s access token", self.provider)
                tokens = await refresh_tokens(self.provider, self.settings, tokens.refresh_token)
                await self._persist(tokens)
            self._cached = tokens
            return tokens.access_token

    def invalidate(self) -> None:
        """Force a reload from the database on the next call."""
        self._cached = None

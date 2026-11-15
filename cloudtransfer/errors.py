"""Exception hierarchy shared by providers and the transfer engine."""

from __future__ import annotations


class CloudTransferError(Exception):
    """Base class for every error this application raises deliberately."""


class ConfigError(CloudTransferError):
    """Configuration is missing or invalid."""


class AuthError(CloudTransferError):
    """An account is not connected, or its tokens can no longer be refreshed."""


class ProviderError(CloudTransferError):
    """A provider returned an error response.

    ``retryable`` tells the engine whether backing off and trying again could
    plausibly help. ``retry_after`` carries the provider's own hint in seconds.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = super().__str__()
        if self.status is not None:
            return f"[HTTP {self.status}] {base}"
        return base


class RetryableError(ProviderError):
    """Shorthand for a provider error that is worth retrying."""

    def __init__(self, message: str, **kw) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class NotFoundError(ProviderError):
    """The requested item does not exist (or is not visible to this account)."""


class SkipFile(CloudTransferError):
    """This file cannot be transferred and should be recorded as skipped.

    Raised for conditions that will never succeed on retry: a Google Workspace
    type with no export path, a file the account may not download, or a file
    beyond the destination's size limit.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class VerificationError(CloudTransferError):
    """Source and destination hashes disagree after a transfer."""

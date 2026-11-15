"""Settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
import secrets
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError

# OneDrive requires every chunk except the last to be a multiple of 320 KiB.
ONEDRIVE_CHUNK_MULTIPLE = 327_680

# Microsoft Graph refuses upload-session chunks above 60 MiB.
ONEDRIVE_MAX_CHUNK = 60 * 1024 * 1024

# Below this, a single PUT to /content is cheaper than an upload session.
# Microsoft's guidance is to switch to an upload session above 4 MiB, so that
# is both the default and the ceiling.
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024

# OneDrive's own per-file ceiling.
ONEDRIVE_MAX_FILE_SIZE = 250 * 1024 * 1024 * 1024


APP_NAME = "CloudTransfer"


def platform_data_dir(app_name: str = APP_NAME) -> Path:
    """The per-user directory this platform expects application data in."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / app_name


def default_data_dir() -> Path:
    """Where to keep the database when nothing was configured.

    A packaged build is installed somewhere read-only, such as Program Files,
    so a relative ``./data`` would fail on first write. Running from a checkout
    keeps it beside the source, which is what a developer expects.
    """
    if getattr(sys, "frozen", False):
        return platform_data_dir()
    return Path("./data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str = "http://localhost:8080"

    google_client_id: str = ""
    google_client_secret: str = ""

    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_tenant: str = "common"

    secret_key: str = ""
    data_dir: Path = Field(default_factory=default_data_dir)

    chunk_size: int = 10 * 1024 * 1024
    simple_upload_max: int = SIMPLE_UPLOAD_MAX
    max_parallel_files: int = 3
    max_retries: int = 6
    verify_hashes: bool = True
    acknowledge_abuse: bool = False

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = Field(default="INFO")

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("chunk_size")
    @classmethod
    def _check_chunk(cls, v: int) -> int:
        if v % ONEDRIVE_CHUNK_MULTIPLE != 0:
            raise ValueError(
                f"CT_CHUNK_SIZE must be a multiple of {ONEDRIVE_CHUNK_MULTIPLE} "
                f"(320 KiB) because Microsoft Graph rejects other sizes; got {v}"
            )
        if v > ONEDRIVE_MAX_CHUNK:
            raise ValueError(
                f"CT_CHUNK_SIZE must not exceed {ONEDRIVE_MAX_CHUNK} bytes (60 MiB); got {v}"
            )
        if v <= 0:
            raise ValueError("CT_CHUNK_SIZE must be positive")
        return v

    @field_validator("simple_upload_max")
    @classmethod
    def _check_simple_max(cls, v: int) -> int:
        if not 0 <= v <= SIMPLE_UPLOAD_MAX:
            raise ValueError(
                f"CT_SIMPLE_UPLOAD_MAX must be between 0 and {SIMPLE_UPLOAD_MAX} "
                f"(4 MiB), above which Microsoft wants an upload session; got {v}"
            )
        return v

    @field_validator("max_parallel_files")
    @classmethod
    def _check_parallel(cls, v: int) -> int:
        if not 1 <= v <= 32:
            raise ValueError("CT_MAX_PARALLEL_FILES must be between 1 and 32")
        return v

    # -- derived ------------------------------------------------------------

    @property
    def base_url_is_explicit(self) -> bool:
        """True when CT_BASE_URL was set rather than left at its default.

        A server behind a proxy must use the configured public address, since
        the request itself arrives as plain http. The desktop build has no such
        proxy and may run on whichever port is free, so there the request's own
        address is the accurate one.
        """
        return "base_url" in self.model_fields_set

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cloudtransfer.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/google/callback"

    @property
    def ms_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/microsoft/callback"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def ms_configured(self) -> bool:
        return bool(self.ms_client_id and self.ms_client_secret)

    def resolve_secret_key(self) -> str:
        """Return the secret key, generating and persisting one if unset.

        A generated key lives in ``<data_dir>/secret.key`` with owner-only
        permissions. Losing it invalidates stored refresh tokens, which only
        means reconnecting the two accounts.
        """
        if self.secret_key:
            return self.secret_key
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key_file = self.data_dir / "secret.key"
        if key_file.exists():
            return key_file.read_text(encoding="utf-8").strip()
        generated = secrets.token_urlsafe(48)
        key_file.write_text(generated, encoding="utf-8")
        key_file.chmod(0o600)
        return generated

    def require_provider(self, provider: str) -> None:
        if provider == "google" and not self.google_configured:
            raise ConfigError(
                "Google is not configured. Set CT_GOOGLE_CLIENT_ID and "
                "CT_GOOGLE_CLIENT_SECRET (see .env.example)."
            )
        if provider == "microsoft" and not self.ms_configured:
            raise ConfigError(
                "Microsoft is not configured. Set CT_MS_CLIENT_ID and "
                "CT_MS_CLIENT_SECRET (see .env.example)."
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir = settings.data_dir.expanduser().resolve()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings

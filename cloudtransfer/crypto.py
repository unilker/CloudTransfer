"""Symmetric encryption for OAuth refresh tokens held in the database.

A refresh token is a long-lived key to someone's entire Drive or OneDrive, so
it does not sit in SQLite in plaintext. The Fernet key is derived from
``CT_SECRET_KEY``; losing that key costs nothing but reconnecting the accounts.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .errors import AuthError


def _fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(secret: str, plaintext: str) -> str:
    return _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(secret: str, ciphertext: str) -> str:
    try:
        return _fernet(secret).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise AuthError(
            "Stored credentials could not be decrypted. This usually means "
            "CT_SECRET_KEY changed; reconnect the account to fix it."
        ) from exc

"""Token encryption at rest."""

from __future__ import annotations

import pytest

from cloudtransfer.crypto import decrypt, encrypt
from cloudtransfer.errors import AuthError


def test_round_trip():
    token = "1//0eXaMpLe-refresh-token_with.chars"
    assert decrypt("secret", encrypt("secret", token)) == token


def test_ciphertext_does_not_contain_the_plaintext():
    token = "super-secret-refresh-token"
    assert token not in encrypt("key", token)


def test_the_same_token_encrypts_differently_each_time():
    """Fernet includes a random IV, so identical tokens must not look alike."""
    first = encrypt("key", "same")
    second = encrypt("key", "same")
    assert first != second
    assert decrypt("key", first) == decrypt("key", second) == "same"


def test_a_different_key_cannot_decrypt():
    blob = encrypt("key-one", "token")
    with pytest.raises(AuthError, match="CT_SECRET_KEY changed"):
        decrypt("key-two", blob)


def test_tampered_ciphertext_is_rejected():
    blob = encrypt("key", "token")
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    with pytest.raises(AuthError):
        decrypt("key", tampered)


def test_garbage_is_rejected_cleanly():
    with pytest.raises(AuthError):
        decrypt("key", "not base64 at all !!!")


def test_unicode_survives_the_round_trip():
    token = "tökén-with-ünïcode-日本語"
    assert decrypt("k", encrypt("k", token)) == token

"""Settings validation - the chunk-size rule above all."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cloudtransfer.config import ONEDRIVE_CHUNK_MULTIPLE, SIMPLE_UPLOAD_MAX, Settings
from cloudtransfer.errors import ConfigError


def test_the_default_chunk_size_is_a_legal_graph_chunk():
    assert Settings().chunk_size % ONEDRIVE_CHUNK_MULTIPLE == 0


@pytest.mark.parametrize("size", [1, 1000, 1024 * 1024, ONEDRIVE_CHUNK_MULTIPLE + 1])
def test_chunk_sizes_graph_would_reject_are_refused_at_startup(size):
    """Better a clear error now than a 400 from Graph on every upload."""
    with pytest.raises(ValidationError, match="multiple of"):
        Settings(chunk_size=size)


@pytest.mark.parametrize("multiplier", [1, 2, 32, 192])
def test_legal_multiples_are_accepted(multiplier):
    size = ONEDRIVE_CHUNK_MULTIPLE * multiplier
    if size > 60 * 1024 * 1024:
        pytest.skip("above Graph's 60 MiB chunk ceiling")
    assert Settings(chunk_size=size).chunk_size == size


def test_chunks_above_graphs_ceiling_are_refused():
    with pytest.raises(ValidationError, match="60 MiB"):
        Settings(chunk_size=ONEDRIVE_CHUNK_MULTIPLE * 200)


def test_simple_upload_threshold_is_bounded():
    assert Settings(simple_upload_max=0).simple_upload_max == 0
    with pytest.raises(ValidationError, match="4 MiB"):
        Settings(simple_upload_max=SIMPLE_UPLOAD_MAX + 1)


@pytest.mark.parametrize("value", [0, -1, 33])
def test_parallelism_is_bounded(value):
    with pytest.raises(ValidationError, match="between 1 and 32"):
        Settings(max_parallel_files=value)


def test_base_url_loses_its_trailing_slash():
    settings = Settings(base_url="https://transfer.example.com/")
    assert settings.base_url == "https://transfer.example.com"
    assert settings.google_redirect_uri == (
        "https://transfer.example.com/auth/google/callback"
    )
    assert settings.ms_redirect_uri == (
        "https://transfer.example.com/auth/microsoft/callback"
    )


def test_unconfigured_providers_are_reported_clearly(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.google_configured is False
    assert settings.ms_configured is False

    with pytest.raises(ConfigError, match="CT_GOOGLE_CLIENT_ID"):
        settings.require_provider("google")
    with pytest.raises(ConfigError, match="CT_MS_CLIENT_ID"):
        settings.require_provider("microsoft")


def test_configured_providers_pass_the_check(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        google_client_id="gid", google_client_secret="gsecret",
        ms_client_id="mid", ms_client_secret="msecret",
    )
    settings.require_provider("google")
    settings.require_provider("microsoft")


def test_a_secret_key_is_generated_and_reused(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key="")
    first = settings.resolve_secret_key()
    assert first
    assert (tmp_path / "secret.key").exists()
    assert (tmp_path / "secret.key").stat().st_mode & 0o777 == 0o600
    # A second call must not rotate the key, or stored tokens would break.
    assert Settings(data_dir=tmp_path, secret_key="").resolve_secret_key() == first


def test_an_explicit_secret_key_wins(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key="explicit")
    assert settings.resolve_secret_key() == "explicit"
    assert not (tmp_path / "secret.key").exists()

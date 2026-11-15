"""The desktop launcher: port selection, logging, and console-less survival."""

from __future__ import annotations

import io
import logging
import socket
import sys

import pytest

from cloudtransfer.config import platform_data_dir
from cloudtransfer.desktop import (
    DEFAULT_PORT,
    HOST,
    PORT_SCAN_LIMIT,
    _NullStream,
    already_running,
    configure_file_logging,
    ensure_standard_streams,
    find_port,
    port_is_free,
    wait_until_serving,
)


@pytest.fixture
def taken_port():
    """Hold a port open for the duration of a test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        yield port
    finally:
        sock.close()


# ----------------------------------------------------------------- ports


def test_a_free_port_is_reported_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        port = probe.getsockname()[1]
    # The socket is closed again, so the port is free once more.
    assert port_is_free(port) is True


def test_a_port_in_use_is_reported_taken(taken_port):
    assert port_is_free(taken_port) is False


def test_find_port_prefers_the_one_it_was_given():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        free = probe.getsockname()[1]
    assert find_port(free) == free


def test_find_port_moves_past_a_busy_one(taken_port):
    """A stable port is preferred, but a clash must not stop the app starting."""
    chosen = find_port(taken_port)
    assert chosen != taken_port
    assert taken_port < chosen <= taken_port + PORT_SCAN_LIMIT


def test_find_port_falls_back_when_the_whole_range_is_busy(monkeypatch):
    monkeypatch.setattr("cloudtransfer.desktop.port_is_free", lambda *a, **k: False)
    chosen = find_port(DEFAULT_PORT)
    # The OS is asked for anything at all rather than giving up.
    assert isinstance(chosen, int)
    assert chosen > 0


def test_already_running_detects_a_listener(taken_port):
    assert already_running(taken_port) is True


def test_already_running_is_false_for_a_dead_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        port = probe.getsockname()[1]
    assert already_running(port) is False


def test_wait_until_serving_gives_up_rather_than_hanging():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        port = probe.getsockname()[1]
    assert wait_until_serving(port, timeout=0.5) is False


def test_wait_until_serving_returns_immediately_when_up(taken_port):
    assert wait_until_serving(taken_port, timeout=5.0) is True


def test_the_default_port_is_a_fixed_choice():
    """It is deliberate: a registered redirect URI has to keep working."""
    assert DEFAULT_PORT == 8765


# ------------------------------------------------------------- no console


def test_missing_streams_are_replaced(monkeypatch):
    """A windowed build leaves these as None, and libraries write to them."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    ensure_standard_streams()

    assert isinstance(sys.stdout, _NullStream)
    assert isinstance(sys.stderr, _NullStream)
    # Must behave enough like a stream that logging does not raise.
    handler = logging.StreamHandler()
    handler.emit(logging.LogRecord("t", logging.INFO, "f", 1, "message", None, None))
    print("this must not raise")


def test_existing_streams_are_left_alone(monkeypatch):
    sentinel = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sentinel)
    monkeypatch.setattr(sys, "stderr", sentinel)
    ensure_standard_streams()
    assert sys.stdout is sentinel
    assert sys.stderr is sentinel


# ---------------------------------------------------------------- logging


def test_logging_goes_to_a_file_in_the_data_directory(tmp_path):
    """The only diagnostics a windowed build can offer."""
    log_path = configure_file_logging(tmp_path / "data")

    assert log_path.parent.exists()
    logging.getLogger("cloudtransfer.test").warning("a recorded message")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_path.exists()
    assert "a recorded message" in log_path.read_text(encoding="utf-8")

    # Leave the root logger as it was for the rest of the suite.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "baseFilename", None) == str(log_path):
            root.removeHandler(handler)
            handler.close()


# ------------------------------------------------------- data directory


def test_the_data_directory_is_per_user_and_named():
    path = platform_data_dir()
    assert path.name == "CloudTransfer"
    # Never the working directory: a packaged build is installed read-only.
    assert path.is_absolute()


def test_a_frozen_build_does_not_use_the_working_directory(monkeypatch):
    import importlib

    import cloudtransfer.config as config_module

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    reloaded = importlib.reload(config_module)
    try:
        assert reloaded.default_data_dir() == reloaded.platform_data_dir()
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)
        importlib.reload(config_module)

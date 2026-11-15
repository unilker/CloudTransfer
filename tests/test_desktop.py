"""The desktop launcher: port selection, logging, and console-less survival."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import pathlib
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cloudtransfer.config import platform_data_dir
from cloudtransfer.desktop import (
    APP_MUTEX_NAME,
    DEFAULT_PORT,
    HOST,
    PORT_SCAN_LIMIT,
    SingleInstanceLock,
    _NullStream,
    already_running,
    clear_runtime,
    configure_file_logging,
    ensure_standard_streams,
    find_port,
    find_running_instance,
    is_our_server,
    port_is_free,
    read_runtime,
    runtime_path,
    show_error,
    wait_until_serving,
    write_runtime,
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


# ------------------------------------------------------ single-instance lock


def test_the_lock_can_be_taken_and_released():
    lock = SingleInstanceLock()
    assert lock.acquire() is True
    assert lock.already_running is False
    lock.release()
    assert lock.handle is None


def test_releasing_twice_is_harmless():
    lock = SingleInstanceLock()
    lock.acquire()
    lock.release()
    lock.release()  # must not raise


def test_the_lock_works_as_a_context_manager():
    with SingleInstanceLock() as lock:
        assert lock.name == APP_MUTEX_NAME
    assert lock.handle is None


def test_the_lock_never_blocks_startup_when_it_cannot_be_created(monkeypatch):
    """A failure here must not stop the application running."""
    monkeypatch.setattr(sys, "platform", "win32")

    def explode(*_args, **_kwargs):
        raise OSError("no kernel32 here")

    monkeypatch.setattr("ctypes.WinDLL", explode, raising=False)
    lock = SingleInstanceLock()
    assert lock.acquire() is True
    assert lock.already_running is False


def test_the_installer_waits_for_the_same_mutex_the_app_holds():
    """Two files, one name. If they drift, the installer silently stops
    detecting the running application and goes back to failing on locked
    files - which is the bug this pair exists to prevent."""
    iss = (
        pathlib.Path(__file__).resolve().parents[1] / "packaging" / "installer.iss"
    ).read_text(encoding="utf-8")

    declared = [
        line.split("=", 1)[1].strip()
        for line in iss.splitlines()
        if line.strip().startswith("AppMutex=")
    ]
    assert declared == [APP_MUTEX_NAME], (
        f"installer.iss declares {declared}, the application holds {APP_MUTEX_NAME!r}"
    )


def test_the_installer_also_asks_restart_manager_to_close_holders():
    iss = (
        pathlib.Path(__file__).resolve().parents[1] / "packaging" / "installer.iss"
    ).read_text(encoding="utf-8")
    assert "CloseApplications=yes" in iss
    # Two installers at once would fight over the same files.
    assert "SetupMutex=" in iss


def test_the_running_app_message_says_where_to_look():
    """The application has no window, so "close all instances" is not enough."""
    iss = (
        pathlib.Path(__file__).resolve().parents[1] / "packaging" / "installer.iss"
    ).read_text(encoding="utf-8")
    message = next(
        line for line in iss.splitlines()
        if line.startswith("english.SetupAppRunningError=")
    )
    assert "notification area" in message
    assert "Task Manager" in message
    assert "CloudTransfer.exe" in message


# ------------------------------------------------- finding a running instance


class _Handler(BaseHTTPRequestHandler):
    """Stands in for whatever happens to be listening on a port."""

    payload = b'{"status": "ok", "version": "1.0.0"}'

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/healthz":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *_args):
        return


@contextlib.contextmanager
def _serving(payload: bytes | None = None):
    """Run a one-endpoint HTTP server on a free port."""
    handler = type("H", (_Handler,), {"payload": payload or _Handler.payload})
    server = HTTPServer((HOST, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_our_own_server_is_recognised():
    with _serving() as port:
        assert is_our_server(port) is True


def test_a_stranger_on_the_port_is_not_mistaken_for_us():
    """Port 8765 could belong to anything; opening it would be worse than useless."""
    with _serving(payload=b'{"hello": "some other service"}') as port:
        assert is_our_server(port) is False


def test_a_dead_port_is_not_ours():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        port = probe.getsockname()[1]
    assert is_our_server(port, timeout=0.5) is False


def test_a_socket_that_accepts_but_never_answers_is_not_ours():
    """A bare TCP connect succeeding proves nothing."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert already_running(port) is True   # the old, insufficient check
        assert is_our_server(port, timeout=0.5) is False
    finally:
        listener.close()


def test_a_moved_instance_is_found_through_the_runtime_file(tmp_path):
    """The regression this guards: the second launch opened the default port
    and showed a refused connection when the first had moved elsewhere."""
    with _serving() as port:
        write_runtime(tmp_path, port, f"http://{HOST}:{port}")
        found = find_running_instance(tmp_path, DEFAULT_PORT)
    assert found == f"http://{HOST}:{port}"


def test_no_instance_is_reported_when_nothing_answers(tmp_path):
    assert find_running_instance(tmp_path, DEFAULT_PORT) is None


def test_a_stale_runtime_file_does_not_produce_a_dead_url(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        dead_port = probe.getsockname()[1]
    write_runtime(tmp_path, dead_port, f"http://{HOST}:{dead_port}")
    assert find_running_instance(tmp_path, DEFAULT_PORT) is None


def test_the_runtime_file_round_trips(tmp_path):
    assert read_runtime(tmp_path) is None
    write_runtime(tmp_path, 8765, "http://127.0.0.1:8765")

    recorded = read_runtime(tmp_path)
    assert recorded["port"] == 8765
    assert recorded["url"] == "http://127.0.0.1:8765"
    assert recorded["pid"] == os.getpid()

    clear_runtime(tmp_path)
    assert read_runtime(tmp_path) is None
    clear_runtime(tmp_path)  # removing it twice is harmless


def test_a_corrupt_runtime_file_is_ignored(tmp_path):
    runtime_path(tmp_path).write_text("{not json at all", encoding="utf-8")
    assert read_runtime(tmp_path) is None
    assert find_running_instance(tmp_path, DEFAULT_PORT) is None


def test_a_runtime_file_with_a_nonsense_port_is_ignored(tmp_path):
    runtime_path(tmp_path).write_text('{"port": "eight thousand"}', encoding="utf-8")
    assert find_running_instance(tmp_path, DEFAULT_PORT) is None


def test_errors_are_shown_without_raising(monkeypatch, capsys):
    """A windowed build has no console, so this must never be the failure."""
    monkeypatch.setattr(sys, "platform", "linux")
    show_error("A title", "A message\nover two lines")
    assert "A title" in capsys.readouterr().err

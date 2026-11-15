"""Desktop launcher: the entry point the packaged application starts from.

The program is a local web application, so "running it" means starting the
server on the loopback interface and opening a browser at it. This module adds
the parts a double-clicked application needs and a terminal command does not:

* a writable data directory under the user's profile, since the install
  directory is read-only;
* a log file, because a windowed build has no console for messages to go to;
* a predictable port, so the redirect URI registered with Google and Microsoft
  keeps working between runs;
* a tray icon, so there is something to quit from.

None of it changes how transfers work - the same application object serves the
same routes either way.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

# Chosen rather than taken from the OS so the address stays the same between
# runs: a redirect URI registered once should keep working.
DEFAULT_PORT = 8765
PORT_SCAN_LIMIT = 20

HOST = "127.0.0.1"

# A named mutex held for as long as the application runs. The Windows
# installer looks for exactly this name (AppMutex in installer.iss) so it can
# say "close the application first" instead of failing partway through with a
# file it could not replace. Changing it here means changing it there too.
APP_MUTEX_NAME = "CloudTransferSingleInstance"

_ERROR_ALREADY_EXISTS = 183


def port_is_free(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
        return True


def find_port(preferred: int = DEFAULT_PORT, host: str = HOST) -> int:
    """Return the preferred port, or the next free one above it.

    Both providers accept a loopback redirect on any port for desktop-style
    registrations, so moving up is safe; staying put is merely tidier.
    """
    for candidate in range(preferred, preferred + PORT_SCAN_LIMIT):
        if port_is_free(candidate, host):
            return candidate
    # Nothing in the range is free; let the OS pick anything.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def already_running(port: int, host: str = HOST) -> bool:
    """True when something is already serving on the preferred port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


class _NullStream(io.TextIOBase):
    """Somewhere for output to go when the build has no console."""

    def write(self, text: str) -> int:  # pragma: no cover - trivial
        return len(text)

    def flush(self) -> None:  # pragma: no cover - trivial
        return None


def ensure_standard_streams() -> None:
    """Give ``sys.stdout``/``sys.stderr`` a sink when they are missing.

    A windowed build has no console, and PyInstaller leaves these as ``None``.
    ``print`` tolerates that, but any library that writes to them directly -
    uvicorn's logging among them - raises instead, which would kill the
    application at startup with nothing on screen to explain it.
    """
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()


def configure_file_logging(data_dir: Path) -> Path:
    """Send logs to a file; a windowed build has no console to print to."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "cloudtransfer.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return log_path


def wait_until_serving(port: int, timeout: float = 30.0, host: str = HOST) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if already_running(port, host):
            return True
        time.sleep(0.15)
    return False


class SingleInstanceLock:
    """Holds a named Windows mutex for the lifetime of the process.

    Two jobs in one object: a second launch can tell that the application is
    already running, and the installer can tell the same thing, which is what
    lets it ask the user to close it rather than tripping over locked files.

    On anything other than Windows this does nothing and reports nothing,
    since there is no installer there to care.
    """

    def __init__(self, name: str = APP_MUTEX_NAME) -> None:
        self.name = name
        self.handle = None
        self.already_running = False

    def acquire(self) -> bool:
        """Take the lock. Returns False if another instance already holds it."""
        if sys.platform != "win32":
            return True
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_bool,
                ctypes.c_wchar_p,
            ]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            # Session-local, not "Global\\": creating a global object needs a
            # privilege a normal user account does not have, and the installer
            # runs in the same session anyway.
            handle = kernel32.CreateMutexW(None, False, self.name)
            last_error = ctypes.get_last_error()
            if not handle:
                log.debug("could not create the single-instance mutex (%d)", last_error)
                return True
            self.handle = handle
            self.already_running = last_error == _ERROR_ALREADY_EXISTS
            return not self.already_running
        except Exception as exc:  # pragma: no cover - never fatal
            log.debug("single-instance lock unavailable: %s", exc)
            return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(self.handle)
        except Exception:  # pragma: no cover - process is ending anyway
            pass
        finally:
            self.handle = None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


class ServerThread(threading.Thread):
    """Runs uvicorn in the background so the tray loop can own the main thread."""

    def __init__(self, port: int, host: str = HOST) -> None:
        super().__init__(name="cloudtransfer-server", daemon=True)
        self.port = port
        self.host = host
        self._server = None
        self._error: BaseException | None = None

    def run(self) -> None:  # pragma: no cover - exercised by the launcher
        import uvicorn

        from .web.app import app

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        try:
            self._server.run()
        except BaseException as exc:  # noqa: BLE001 - reported to the user
            self._error = exc
            log.exception("the server stopped unexpectedly")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

    @property
    def error(self) -> BaseException | None:
        return self._error


def _tray_image():
    """Build the tray icon in memory, so no image file has to be installed."""
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    blue = (26, 102, 214, 255)
    white = (255, 255, 255, 255)
    draw.ellipse((2, 2, size - 3, size - 3), fill=blue)
    # A right-pointing arrow: the transfer direction.
    draw.polygon([(20, 26), (36, 26), (36, 18), (50, 32), (36, 46), (36, 38), (20, 38)],
                 fill=white)
    return image


def _run_tray(url: str, server: ServerThread) -> int:  # pragma: no cover - UI loop
    import pystray

    def on_open(_icon=None, _item=None) -> None:
        webbrowser.open(url)

    def on_quit(icon, _item=None) -> None:
        icon.visible = False
        icon.stop()

    icon = pystray.Icon(
        "cloudtransfer",
        _tray_image(),
        "CloudTransfer",
        menu=pystray.Menu(
            pystray.MenuItem("Open CloudTransfer", on_open, default=True),
            pystray.MenuItem("Quit", on_quit),
        ),
    )
    icon.run()
    server.stop()
    server.join(timeout=10)
    return 0


def _run_console(url: str, server: ServerThread) -> int:  # pragma: no cover - UI loop
    print(f"CloudTransfer is running at {url}")
    print("Leave this window open while transfers run. Press Ctrl+C to stop.")
    try:
        while server.is_alive():
            server.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    server.stop()
    server.join(timeout=10)
    return 0


def run_desktop(
    *,
    port: int | None = None,
    open_browser: bool = True,
    tray: bool = True,
) -> int:
    """Start the local server and hand control to the tray (or the console)."""
    ensure_standard_streams()

    # Chosen before settings are read, because a packaged build is installed
    # somewhere the user cannot write to.
    from .config import get_settings, platform_data_dir

    os.environ.setdefault("CT_DATA_DIR", str(platform_data_dir()))
    settings = get_settings()
    log_path = configure_file_logging(settings.data_dir)

    preferred = port or int(os.environ.get("CT_PORT") or DEFAULT_PORT)

    # Held until the process exits, so the installer can see we are running.
    lock = SingleInstanceLock()
    lock.acquire()

    # A second launch should bring the first one forward, not fail to bind.
    if lock.already_running or already_running(preferred):
        url = f"http://{HOST}:{preferred}"
        log.info("CloudTransfer is already running at %s", url)
        if open_browser:
            webbrowser.open(url)
        print(f"CloudTransfer is already running at {url}")
        lock.release()
        return 0

    chosen = find_port(preferred)
    url = f"http://{HOST}:{chosen}"
    # Redirect URIs are built from the address the browser actually used, and
    # the setup page shows the result, so a moved port stays self-consistent.
    os.environ["CT_PORT"] = str(chosen)

    server = ServerThread(chosen)
    server.start()

    if not wait_until_serving(chosen):
        server.stop()
        lock.release()
        message = (
            f"CloudTransfer could not start on {url}.\n"
            f"See the log at {log_path} for details."
        )
        print(message, file=sys.stderr)
        log.error("server did not come up on port %d", chosen)
        return 1

    log.info("CloudTransfer is serving at %s (log: %s)", url, log_path)
    if open_browser:
        webbrowser.open(url)

    try:
        if tray:
            try:
                return _run_tray(url, server)
            except Exception as exc:  # noqa: BLE001 - headless or no tray
                log.info("no system tray available (%s); staying in the console", exc)
        return _run_console(url, server)
    finally:
        # Release it promptly, so an installer started right after the window
        # closes does not still see the application as running.
        lock.release()

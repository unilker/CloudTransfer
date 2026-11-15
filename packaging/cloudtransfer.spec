# PyInstaller build definition for the desktop application.
#
#   pyinstaller packaging/cloudtransfer.spec --noconfirm
#
# Produces a one-folder build in dist/CloudTransfer. One folder rather than one
# file because a single-file build unpacks itself into a temporary directory on
# every launch, which is both slower and an odd look for a program whose whole
# point is not writing to disk.
#
# Three things need declaring by hand, because nothing imports them in a way
# static analysis can see:
#
#   * the HTML template and the static assets, which are read from the package
#     directory at runtime;
#   * uvicorn's protocol, loop and lifespan implementations, which it imports
#     by name from a string;
#   * the SQLAlchemy async SQLite dialect, which is resolved from the database
#     URL at runtime.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
PACKAGE = ROOT / "cloudtransfer"

ICON_ICO = SPEC_DIR / "cloudtransfer.ico"
ICON_PNG = SPEC_DIR / "cloudtransfer.png"

datas = [
    (str(PACKAGE / "web" / "templates"), "cloudtransfer/web/templates"),
    (str(PACKAGE / "web" / "static"), "cloudtransfer/web/static"),
]

# pystray picks its backend by platform at import time, and importing it on a
# headless build machine fails outright - so name the backends explicitly
# rather than relying on a collect that can silently come back empty.
try:
    pystray_modules = collect_submodules("pystray")
except Exception:
    pystray_modules = []
pystray_modules += [
    "pystray._win32",
    "pystray._darwin",
    "pystray._xorg",
    "pystray._gtk",
    "pystray._appindicator",
    "pystray._dummy",
]

hiddenimports = [
    # uvicorn picks these by name at runtime.
    *collect_submodules("uvicorn"),
    *dict.fromkeys(pystray_modules),
    # Resolved from the "sqlite+aiosqlite://" URL, never imported directly.
    "aiosqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    # Imported lazily by the launcher's logging setup.
    "logging.handlers",
]

excludes = [
    # Nothing in the application draws plots or crunches arrays; leaving these
    # in would add tens of megabytes if they happen to be in the build env.
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "tkinter",
    "pytest",
    "respx",
    "IPython",
]

a = Analysis(
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CloudTransfer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console: the launcher shows a tray icon and logs to a file instead.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_ICO) if ICON_ICO.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CloudTransfer",
)

app = BUNDLE(
    coll,
    name="CloudTransfer.app",
    icon=str(ICON_PNG) if ICON_PNG.exists() else None,
    bundle_identifier="dev.cloudtransfer.app",
    info_plist={
        "CFBundleShortVersionString": "1.0.0",
        "LSBackgroundOnly": False,
        "NSHighResolutionCapable": True,
    },
)

"""Entry script for the packaged build.

PyInstaller needs a plain script to start from. Keeping it separate from the
console entry point means the packaged application defaults to the desktop
launcher while still accepting ``serve`` or ``transfer`` as arguments.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Required before anything spawns a process in a frozen build on Windows,
    # or the child re-runs the launcher instead of the worker.
    multiprocessing.freeze_support()

    from cloudtransfer.cli import main_desktop

    return main_desktop()


if __name__ == "__main__":
    sys.exit(main())

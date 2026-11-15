"""Command line entry points: run the server, or transfer headlessly."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import get_settings

    settings = get_settings()
    uvicorn.run(
        "cloudtransfer.web.app:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


def _transfer(args: argparse.Namespace) -> int:
    """Run one job to completion without the web UI."""
    from .accounts import TokenManager, load_account
    from .config import get_settings
    from .db import init_db, session_scope
    from .engine.worker import get_runner
    from .http import build_client
    from .models import Job, JobStatus
    from .providers.onedrive import OneDriveDest

    async def run() -> int:
        settings = get_settings()
        await init_db(settings)

        for provider, human in (("google", "Google Drive"), ("microsoft", "OneDrive")):
            if await load_account(provider) is None:
                print(
                    f"{human} is not connected. Start the server with "
                    f"'cloudtransfer serve' and connect it in the browser first.",
                    file=sys.stderr,
                )
                return 2

        dest_id = args.dest
        async with build_client() as client:
            dest = OneDriveDest(
                TokenManager("microsoft", settings).get_token, settings, client=client
            )
            if dest_id == "root":
                dest_id = (await dest.get_root()).id

        async with session_scope(settings) as session:
            job = Job(
                status=JobStatus.PENDING,
                source_folder_id=args.source,
                source_folder_name=args.source,
                dest_folder_id=dest_id,
                dest_folder_path=args.dest,
                conflict_policy=args.conflict,
                verify=not args.no_verify,
            )
            session.add(job)
            await session.flush()
            job_id = job.id

        print(f"job {job_id}: {args.source} -> {args.dest}")
        runner = get_runner(settings)
        await runner.start(job_id)

        last = ""
        while runner.is_running(job_id):
            await asyncio.sleep(1.0)
            async with session_scope(settings) as session:
                job = await session.get(Job, job_id)
                line = (
                    f"  {job.status}: {job.done_files}/{job.total_files} files, "
                    f"{job.done_bytes}/{job.total_bytes} bytes "
                    f"({job.percent:.1f}%), {job.failed_files} failed, "
                    f"{job.skipped_files} skipped"
                )
            if line != last:
                print(line)
                last = line

        async with session_scope(settings) as session:
            job = await session.get(Job, job_id)
            print(f"finished: {job.status}")
            if job.error:
                print(f"error: {job.error}", file=sys.stderr)
            failed = job.failed_files
        return 1 if failed else 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloudtransfer",
        description=(
            "Stream files from Google Drive to OneDrive without writing them to disk."
        ),
    )
    parser.add_argument("--version", action="version", version=f"CloudTransfer {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web interface")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="reload on code changes")
    serve.set_defaults(func=_serve)

    transfer = sub.add_parser("transfer", help="run one transfer headlessly")
    transfer.add_argument("--source", required=True, help="Google Drive folder ID")
    transfer.add_argument("--dest", required=True, help="OneDrive folder ID, or 'root'")
    transfer.add_argument(
        "--conflict", choices=["rename", "replace", "skip"], default="rename"
    )
    transfer.add_argument("--no-verify", action="store_true", help="skip hash verification")
    transfer.set_defaults(func=_transfer)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

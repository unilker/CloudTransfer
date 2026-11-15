"""Application factory and lifespan wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..appconfig import setup_status
from ..config import Settings, get_settings
from ..db import dispose_db, healthcheck, init_db
from ..engine.worker import get_runner, reset_stale_jobs
from ..errors import AuthError, CloudTransferError, ConfigError
from . import routes_auth, routes_browse, routes_jobs

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO, which drowns out the transfer log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    await init_db(settings)
    # A previous process may have died mid-transfer; make those jobs resumable
    # rather than leaving them stuck in "running" forever.
    await reset_stale_jobs(settings)
    log.info(
        "CloudTransfer %s ready on %s (chunk %d bytes, %d files in parallel)",
        __version__, settings.base_url, settings.chunk_size, settings.max_parallel_files,
    )
    status = await setup_status(settings)
    missing = [name for name, entry in status.items() if not entry["configured"]]
    if missing:
        # Deliberately no URL: the desktop launcher picks its own port, so the
        # configured base_url would often name an address nothing is serving.
        log.warning(
            "Not set up yet for: %s. Open the interface and finish the setup "
            "step; no file editing is required.",
            ", ".join(missing),
        )
    try:
        yield
    finally:
        await get_runner(settings).shutdown()
        await dispose_db()
        log.info("shut down cleanly")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="CloudTransfer",
        version=__version__,
        summary="Stream files from Google Drive to OneDrive without touching a disk",
        lifespan=lifespan,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.resolve_secret_key(),
        session_cookie="cloudtransfer_session",
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
        max_age=3600,
    )

    app.include_router(routes_auth.router, tags=["auth"])
    app.include_router(routes_browse.router, tags=["browse"])
    app.include_router(routes_jobs.router, tags=["jobs"])

    static_dir = HERE / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(ConfigError)
    async def _config_error(_: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(CloudTransferError)
    async def _app_error(_: Request, exc: CloudTransferError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        ok = await healthcheck()
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "version": __version__},
        )

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        # Setup state is fetched by the page itself, so it stays correct after
        # the wizard is used without needing a reload.
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "chunk_size": settings.chunk_size,
                "parallel": settings.max_parallel_files,
            },
        )

    return app


app = create_app()

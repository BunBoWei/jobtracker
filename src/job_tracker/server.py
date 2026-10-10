"""App factory and uvicorn entry point (python -m job_tracker.server)."""
from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import routes
from .storage import AppNotFound, CorruptCsvError, DataDirMissing, Store, data_dir_from_env

BASE = Path(__file__).parent
LOCAL_HOSTS = {"localhost", "127.0.0.1", "testserver"}  # testserver: Starlette's test client
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_app(data_dir: Path | None = None, today=date.today) -> FastAPI:
    app = FastAPI(title="Job Tracker")
    app.state.store = Store(data_dir or data_dir_from_env(), today=today)
    templates = Jinja2Templates(directory=BASE / "templates")
    templates.env.filters["pct"] = lambda v: "n/a" if v is None else f"{v * 100:.0f}%"
    app.state.templates = templates

    @app.middleware("http")
    async def local_requests_only(request, call_next):
        """Block DNS rebinding (foreign Host) and cross-site form posts (foreign or null Origin)."""
        host = urlparse("//" + request.headers.get("host", "")).hostname
        if host not in LOCAL_HOSTS:
            return PlainTextResponse("Forbidden: unexpected Host header", status_code=403)
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin is not None and urlparse(origin).hostname not in LOCAL_HOSTS:
            return PlainTextResponse("Forbidden: cross-site request", status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(routes.router)

    @app.exception_handler(CorruptCsvError)
    async def corrupt(request, exc):
        return routes.render(request, "error.html", 500, title="A data file could not be read", message=str(exc),
                             hint="The file was not changed. Fix it or restore a copy from the backups folder, then reload.")

    @app.exception_handler(DataDirMissing)
    async def data_dir_missing(request, exc):
        return routes.render(request, "error.html", 500, title="Data folder not found", message=str(exc),
                             hint="Mount Google Drive (or set JOBS_DATA_DIR), then reload.")

    @app.exception_handler(AppNotFound)
    async def not_found(request, exc):
        return routes.render(request, "error.html", 404, title="Application not found", message=str(exc), hint="")

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return routes.render(request, "error.html", 400, title="That didn't work", message=str(exc), hint="Go back and try again.")

    return app


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("JOBS_PORT", "8765")))
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()

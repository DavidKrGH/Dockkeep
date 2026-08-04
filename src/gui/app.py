import hashlib
import logging
import os
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from ..scheduler.owner import SchedulerOwnerManager
from ..services.errors import ServiceError
from ..services.factory import create_services
from ..utils.logging import setup_system_logger
from .filters import format_bytes, format_datetime, format_duration, relative_time
from .routers import config as config_router
from .routers import (
    config_editor,
    dashboard,
    diagnostics,
    help,
    jobs,
    rclone,
    repositories,
    restore,
    runs,
)
from .routers._helpers import fragment_response, template_response, wants_fragment

RUN_SHUTDOWN_TIMEOUT = 10.0
STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def _compute_asset_version(static_dir: Path = STATIC_DIR) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in static_dir.rglob("*") if item.is_file()):
        relative_path = path.relative_to(static_dir).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def _status_label(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _http_error_detail(exc: HTTPException) -> tuple[str, str]:
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or _status_label(exc.status_code).lower().replace(" ", "_"))
        message = str(detail.get("message") or _status_label(exc.status_code))
        return code, message
    if detail:
        return _status_label(exc.status_code).lower().replace(" ", "_"), str(detail)
    return _status_label(exc.status_code).lower().replace(" ", "_"), _status_label(exc.status_code)


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
) -> HTMLResponse:
    context: dict[str, object] = {
        "error": {
            "status_code": status_code,
            "status_label": _status_label(status_code),
            "code": code,
            "message": message,
        }
    }
    if wants_fragment(request):
        return fragment_response(request, "fragments/restore_error.html", context)
    return template_response(request, "error.html", context, status_code=status_code)


def create_app(config_path: Path, appdata_dir: Path | None = None) -> FastAPI:
    setup_system_logger()
    resolved_appdata_dir = appdata_dir or Path(os.environ.get("DK_APPDATA_DIR", "/appdata"))

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        manager = SchedulerOwnerManager(
            config_path,
            appdata_dir=resolved_appdata_dir,
            on_backup_success=app.state.services.backup_stats_collector.collect_and_store_async,
        )
        app.state.scheduler_owner_manager = manager
        manager.start_background()

        yield

        try:
            await app.state.services.run_manager.shutdown(RUN_SHUTDOWN_TIMEOUT)
        except Exception:
            logger.exception("Error during run manager shutdown")
        finally:
            manager.stop()

    app = FastAPI(
        title="Dockkeep",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    app.state.config_path = config_path
    app.state.services = create_services(config_path, appdata_dir=resolved_appdata_dir)
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    templates.env.filters["relative_time"] = relative_time
    templates.env.filters["format_datetime"] = format_datetime
    templates.env.filters["format_duration"] = format_duration
    templates.env.filters["format_bytes"] = format_bytes
    templates.env.globals["asset_version"] = _compute_asset_version()
    app.state.templates = templates
    app.state.scheduler_owner_manager = None

    @app.exception_handler(ServiceError)
    async def _service_error_handler(request: Request, exc: ServiceError) -> HTMLResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> HTMLResponse:
        code, message = _http_error_detail(exc)
        return _error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
        )

    @app.exception_handler(Exception)
    async def _unexpected_exception_handler(request: Request, exc: Exception) -> HTMLResponse:
        logger.exception("Unexpected GUI error", exc_info=(type(exc), exc, exc.__traceback__))
        return _error_response(
            request,
            status_code=500,
            code="unexpected_error",
            message="Unexpected server error",
        )

    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR),
        name="static",
    )

    app.include_router(dashboard.router)
    app.include_router(config_router.router)
    app.include_router(config_editor.router)
    app.include_router(diagnostics.router)
    app.include_router(help.router)
    app.include_router(runs.router)
    app.include_router(jobs.router)
    app.include_router(repositories.router)
    app.include_router(restore.router)
    app.include_router(rclone.router)

    return app

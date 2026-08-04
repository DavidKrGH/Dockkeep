from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...services.runs import RunView
from ._helpers import (
    ResourceName,
    fragment_response,
    get_services,
    trigger_run_status_changed,
)

router = APIRouter(prefix="/jobs")


def _fragment(request: Request, view: RunView) -> HTMLResponse:
    response = fragment_response(request, "fragments/run_status.html", {"run": view})
    return trigger_run_status_changed(response)


async def _start_run(job_name: str, step: str, request: Request, *, dry_run: bool) -> HTMLResponse:
    view = await get_services(request).run_service.start_run_status_view(
        f"{job_name}.{step}",
        action_job=job_name,
        action_step=step,
        dry_run=dry_run,
    )
    return _fragment(request, view)


@router.post("/{job_name}/{step}/run", response_class=HTMLResponse)
async def run_job(job_name: ResourceName, step: str, request: Request) -> HTMLResponse:
    return await _start_run(job_name, step, request, dry_run=False)


@router.post("/{job_name}/{step}/dry-run", response_class=HTMLResponse)
async def run_dry_run_job(job_name: ResourceName, step: str, request: Request) -> HTMLResponse:
    return await _start_run(job_name, step, request, dry_run=True)

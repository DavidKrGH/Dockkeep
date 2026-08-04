from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._helpers import (
    ResourceName,
    fragment_response,
    get_services,
    template_response,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    data = await get_services(request).dashboard_service.get_dashboard_view()
    return template_response(request, "dashboard.html", data)


@router.get("/dashboard/runs", response_class=HTMLResponse)
async def dashboard_runs_panel(request: Request) -> HTMLResponse:
    data = await get_services(request).dashboard_service.get_runs_panel_view()
    return fragment_response(request, "fragments/dashboard_runs.html", data)


@router.get("/dashboard/jobs/{job_name}/status", response_class=HTMLResponse)
async def dashboard_job_status(job_name: ResourceName, request: Request) -> HTMLResponse:
    job = await get_services(request).dashboard_service.get_job_status_view(job_name)
    return fragment_response(request, "fragments/dashboard_job_status.html", {"job": job})

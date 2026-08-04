from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._helpers import template_response

router = APIRouter(prefix="/help")


@router.get("", response_class=HTMLResponse)
async def help_page(request: Request) -> HTMLResponse:
    return template_response(request, "help.html", {})

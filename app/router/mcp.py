# ruff: file-ignore[line-too-long]
"""MCP protocol endpoint (Epic 73, spec A4): ``/api/mcp`` (served as ``{frontend}/mcp``) and the RFC 9728 Protected
Resource Metadata ``/.well-known/oauth-protected-resource[/mcp]``. Mounted by ``app.main`` outside ``/api/v1``.
Bearer-only: no cookie is read and no CSRF applies."""

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.services import mcp_service

router = APIRouter(tags=["mcp"])


@router.api_route("/api/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def mcp_endpoint(request: Request) -> Response:
    return await mcp_service.handle(request)


def _prm() -> JSONResponse:
    body: dict[str, Any] = mcp_service.protected_resource_metadata()
    return JSONResponse(body, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource_mcp() -> JSONResponse:
    return _prm()


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
async def protected_resource_root() -> JSONResponse:
    return _prm()


@router.get("/api/mcp/.well-known/oauth-protected-resource", include_in_schema=False)
async def protected_resource_api() -> JSONResponse:
    return _prm()

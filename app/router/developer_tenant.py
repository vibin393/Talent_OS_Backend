# ruff: file-ignore[line-too-long]
"""Tenant workspace of the developer portal (Epic 74, spec A4; ``/api/v1/developer-tenant``): Tenant Developers on the
normal ``tos_session`` with ``app:develop`` (human users only): the Tenant's own sandboxes (``owner_kind='tenant'``,
at most 2), the playground and conformance runs of its private Apps. App CRUD stays on Epic 67's ``/api/v1/apps``."""

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.errors import Forbidden
from app.core.permissions import Principal, require
from app.dependencies.auth import CurrentUserDep
from app.schemas.developer import (
    AppUsageOut,
    HandoffOut,
    PlaygroundExchangeIn,
    PlaygroundTokenIn,
    PlaygroundTokenOut,
    RunOut,
    RunStartIn,
    SandboxCreate,
    SandboxInstallIn,
    SandboxInstallOut,
    SandboxOut,
    SandboxResetIn,
)
from app.services import developer_portal_service as dev

router = APIRouter(prefix="/developer-tenant", tags=["developer-tenant"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _human(principal: CurrentUserDep) -> Principal:
    if principal.type != "user":
        raise Forbidden("Sign in to Talent OS to use the developer workspace", code="human_session_required")
    return principal


Human = Annotated[Principal, Depends(_human)]
Develop = Annotated[Principal, Depends(require("app:develop"))]


async def _owner() -> dev.Owner:
    return await dev.tenant_owner(get_context())


@router.get("/sandboxes", response_model=list[SandboxOut])
async def list_sandboxes(_h: Human, _d: Develop) -> list[SandboxOut]:
    return await dev.list_sandboxes(await _owner())


@router.post("/sandboxes", response_model=SandboxOut, status_code=status.HTTP_201_CREATED)
async def create_sandbox(body: SandboxCreate, _h: Human, _d: Develop) -> SandboxOut:
    return await dev.create_sandbox(await _owner(), body)


@router.post("/sandboxes/{sandbox_id}/reset", response_model=SandboxOut)
async def reset_sandbox(sandbox_id: uuid.UUID, body: SandboxResetIn, _h: Human, _d: Develop) -> SandboxOut:
    return await dev.reset_sandbox(await _owner(), sandbox_id, reinstall_apps=body.reinstall_apps)


@router.delete("/sandboxes/{sandbox_id}", response_model=SandboxOut)
async def delete_sandbox(sandbox_id: uuid.UUID, _h: Human, _d: Develop) -> SandboxOut:
    return await dev.delete_sandbox(await _owner(), sandbox_id)


@router.post("/sandboxes/{sandbox_id}/open", response_model=HandoffOut)
async def open_sandbox(sandbox_id: uuid.UUID, _h: Human, _d: Develop, response: Response) -> HandoffOut:
    response.headers.update(NO_STORE)
    return await dev.open_sandbox(await _owner(), sandbox_id)


@router.post("/sandboxes/{sandbox_id}/installations", response_model=SandboxInstallOut, status_code=status.HTTP_201_CREATED)
async def install_in_sandbox(sandbox_id: uuid.UUID, body: SandboxInstallIn, _h: Human, _d: Develop) -> SandboxInstallOut:
    return await dev.install_in_sandbox(await _owner(), sandbox_id, body)


@router.post("/playground/token", response_model=PlaygroundTokenOut)
async def playground_token(body: PlaygroundTokenIn, _h: Human, _d: Develop, response: Response) -> PlaygroundTokenOut:
    response.headers.update(NO_STORE)
    return await dev.playground_token(await _owner(), body)


@router.post("/playground/exchange", response_model=PlaygroundTokenOut)
async def playground_exchange(body: PlaygroundExchangeIn, _h: Human, _d: Develop, response: Response) -> PlaygroundTokenOut:
    response.headers.update(NO_STORE)
    return await dev.playground_exchange(await _owner(), body)


@router.post("/apps/{app_id}/conformance-runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def start_run(app_id: uuid.UUID, body: RunStartIn, _h: Human, _d: Develop) -> RunOut:
    return await dev.start_run(await _owner(), app_id, body.sandbox_id)


@router.get("/apps/{app_id}/conformance-runs", response_model=list[RunOut])
async def list_runs(app_id: uuid.UUID, _h: Human, _d: Develop) -> list[RunOut]:
    return await dev.list_runs(await _owner(), app_id)


@router.post("/conformance-runs/{run_id}/finish", response_model=RunOut)
async def finish_run(run_id: uuid.UUID, _h: Human, _d: Develop) -> RunOut:
    return await dev.finish_run(await _owner(), run_id)


@router.get("/conformance-runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, _h: Human, _d: Develop) -> RunOut:
    return await dev.get_run(await _owner(), run_id)


@router.get("/conformance-runs/{run_id}/report")
async def download_report(run_id: uuid.UUID, _h: Human, _d: Develop) -> dict[str, Any]:
    return await dev.download_report(await _owner(), run_id)


@router.get("/apps/{app_id}/usage", response_model=AppUsageOut)
async def app_usage(
    app_id: uuid.UUID, _h: Human, _d: Develop, date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
) -> AppUsageOut:
    end = date_to or dt.date.today()
    return await dev.app_usage(await _owner(), app_id, date_from or end - dt.timedelta(days=29), end)

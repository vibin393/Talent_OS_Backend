"""Provider connector configuration and job monitor (Epic 13, AD-29; ``connector:configure``).
Credentials and webhook secrets are write-only. Each endpoint calls one service function."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.connectors import (
    Category,
    ConnectorConfigIn,
    ConnectorConfigOut,
    ConnectorConfigUpdate,
    ConnectorJobOut,
    JobStatus,
)
from app.services import connectors_service as svc

router = APIRouter(prefix="/connectors", tags=["connectors"])

Configure = Annotated[Principal, Depends(require("connector:configure"))]
DB = TenantDBSessionDep


@router.get("/configs", response_model=list[ConnectorConfigOut])
async def list_configs(_: Configure, db: DB) -> Any:
    return await svc.list_configs(get_context(), db)


@router.post("/configs", response_model=ConnectorConfigOut, status_code=status.HTTP_201_CREATED)
async def create_config(body: ConnectorConfigIn, _: Configure, db: DB) -> Any:
    return await svc.create_config(get_context(), db, body)


@router.patch("/configs/{config_id}", response_model=ConnectorConfigOut)
async def update_config(config_id: uuid.UUID, body: ConnectorConfigUpdate, _: Configure, db: DB) -> Any:
    return await svc.update_config(get_context(), db, config_id, body)


@router.get("/jobs", response_model=list[ConnectorJobOut])
async def list_jobs(
    _: Configure,
    db: DB,
    category: Annotated[Category | None, Query()] = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> Any:
    return await svc.pending(get_context(), db, category, job_status)


@router.post("/jobs/{job_id}/retry", response_model=ConnectorJobOut)
async def retry_job(job_id: uuid.UUID, _: Configure, db: DB) -> Any:
    return await svc.retry(get_context(), db, job_id)

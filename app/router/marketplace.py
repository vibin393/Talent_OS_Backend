# ruff: file-ignore[line-too-long]
"""Integration Marketplace (Epic 69, Part C tenant; ``/api/v1/marketplace``). Reads need ``integration:view``, writes
``integration:manage``. Installing, upgrading, resuming, testing, uninstalling and pre-approving people are human-only
(★ in the service: API keys, app tokens and automated actors get 403 ``human_only_action``). No endpoint returns a
secret value."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core import rate_limit
from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.connectors import ConnectorJobOut
from app.schemas.marketplace import (
    ConfigureIn,
    DataFlowQuery,
    DataFlowRow,
    HealthLogOut,
    InstallationOut,
    InstallationQuery,
    InstallIn,
    ListingOut,
    ListingPageOut,
    ListingQuery,
    ListingVersionLineOut,
    PolicyOut,
    ProcessorOut,
    ReasonIn,
    TestResultOut,
    UninstallIn,
    UpgradeDiffOut,
    UpgradeIn,
    UserAuthorizationsIn,
)
from app.services import marketplace_service

router = APIRouter(prefix="/marketplace", tags=["marketplace"])
DB = TenantDBSessionDep
View = Annotated[Principal, Depends(require("integration:view"))]
Manage = Annotated[Principal, Depends(require("integration:manage"))]
Idem = Annotated[IdempotencyGuard, Depends(idempotency())]


@router.get("/listings", response_model=ListingPageOut)
async def listings(
    _: View,
    db: DB,
    category: str | None = None,
    publisher: str | None = None,
    q: str | None = Query(default=None, max_length=100),
    installed: bool | None = None,
    page: int = Query(1, ge=1, le=100),
    page_size: int = Query(50, ge=1, le=200),
) -> ListingPageOut:
    query = ListingQuery(
        category=category, publisher=publisher, q=q, installed=installed, page=page, page_size=page_size
    )  # type: ignore[arg-type]
    return await marketplace_service.list_listings(get_context(), db, query)


@router.get("/listings/{slug}", response_model=ListingOut)
async def listing(slug: str, _: View, db: DB) -> ListingOut:
    return await marketplace_service.get_listing(get_context(), db, slug)


@router.get("/listings/{slug}/versions", response_model=list[ListingVersionLineOut])
async def listing_versions(slug: str, _: View, db: DB) -> list[ListingVersionLineOut]:
    return (await marketplace_service.get_listing(get_context(), db, slug)).versions


@router.post("/installations", response_model=InstallationOut, status_code=status.HTTP_201_CREATED)
async def install(body: InstallIn, principal: Manage, db: DB, idem: Idem) -> Any:
    await rate_limit.enforce("integration_change", str(principal.tenant_id), 30, 3600)
    return await idem.run(lambda: marketplace_service.install(get_context(), db, body))


@router.get("/installations", response_model=list[InstallationOut])
async def installations(
    _: View,
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    kind: str | None = None,
    category: str | None = None,
    include_uninstalled: bool = False,
) -> list[InstallationOut]:
    q = InstallationQuery(
        status=status_, kind=kind, category=category, include_uninstalled=include_uninstalled, limit=200
    )  # type: ignore[arg-type]
    return (await marketplace_service.list_installations(get_context(), db, q)).data


@router.get("/installations/{installation_id}", response_model=InstallationOut)
async def installation(installation_id: uuid.UUID, _: View, db: DB) -> InstallationOut:
    return await marketplace_service.get(get_context(), db, installation_id)


@router.patch("/installations/{installation_id}", response_model=InstallationOut)
async def configure(
    installation_id: uuid.UUID, body: ConfigureIn, principal: Manage, db: DB
) -> InstallationOut:
    await rate_limit.enforce("integration_change", str(principal.tenant_id), 30, 3600)
    return await marketplace_service.configure(get_context(), db, installation_id, body)


@router.post("/installations/{installation_id}/test", response_model=TestResultOut)
async def test_connection(installation_id: uuid.UUID, _: Manage, db: DB) -> TestResultOut:
    return await marketplace_service.test(get_context(), db, installation_id)


@router.post("/installations/{installation_id}/upgrade", response_model=InstallationOut)
async def upgrade(installation_id: uuid.UUID, body: UpgradeIn, _: Manage, db: DB) -> InstallationOut:
    return await marketplace_service.upgrade(
        get_context(), db, installation_id, accept_manifest_sha256=body.accept_manifest_sha256
    )


@router.get("/installations/{installation_id}/upgrade-diff", response_model=UpgradeDiffOut)
async def upgrade_diff(installation_id: uuid.UUID, _: View, db: DB) -> UpgradeDiffOut:
    return await marketplace_service.upgrade_diff(get_context(), db, installation_id)


@router.post("/installations/{installation_id}/suspend", response_model=InstallationOut)
async def suspend(installation_id: uuid.UUID, body: ReasonIn, _: Manage, db: DB) -> InstallationOut:
    return await marketplace_service.set_status(get_context(), db, installation_id, "suspended", body.reason)


@router.post("/installations/{installation_id}/resume", response_model=InstallationOut)
async def resume(installation_id: uuid.UUID, body: ReasonIn, _: Manage, db: DB) -> InstallationOut:
    from app.core.actors import actor_from_ctx, require_human
    from app.core.errors import ConflictError

    ctx = get_context()
    require_human(actor_from_ctx(ctx))  # ★ resume
    current = await marketplace_service.get(ctx, db, installation_id)
    if current.status != "suspended":
        raise ConflictError("Only suspended integrations can be resumed", code="installation_not_suspended")
    return await marketplace_service.set_status(ctx, db, installation_id, "active", body.reason)


@router.post("/installations/{installation_id}/uninstall", response_model=InstallationOut)
async def uninstall(installation_id: uuid.UUID, body: UninstallIn, _: Manage, db: DB) -> InstallationOut:
    ctx = get_context()
    if body.propagate_erasure:
        await marketplace_service.propagate_erasure(ctx, db, installation_id)
    return await marketplace_service.uninstall(ctx, db, installation_id, reason=body.reason)


@router.get("/installations/{installation_id}/health", response_model=list[HealthLogOut])
async def health(installation_id: uuid.UUID, _: View, db: DB) -> list[HealthLogOut]:
    await marketplace_service.get(get_context(), db, installation_id)
    rows = await marketplace_service.health_history(db, installation_id, 50)
    return [
        HealthLogOut(
            checked_at=r.checked_at,
            trigger=r.trigger,
            result=r.result,
            error_code=r.error_code,
            latency_ms=r.latency_ms,
        )
        for r in rows
    ]


@router.get("/installations/{installation_id}/jobs", response_model=list[ConnectorJobOut])
async def jobs(
    installation_id: uuid.UUID, _: View, db: DB, status_: Annotated[str | None, Query(alias="status")] = None
) -> list[Any]:
    return await marketplace_service.jobs(get_context(), db, installation_id, status_)


@router.post("/installations/{installation_id}/jobs/{job_id}/retry", response_model=ConnectorJobOut)
async def retry_job(installation_id: uuid.UUID, job_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await marketplace_service.retry_job(get_context(), db, installation_id, job_id)


@router.post(
    "/installations/{installation_id}/sync/{job_key}",
    response_model=ConnectorJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_sync(installation_id: uuid.UUID, job_key: str, _: Manage, db: DB) -> Any:
    return await marketplace_service.run_sync(get_context(), db, installation_id, job_key)


@router.post("/installations/{installation_id}/user-authorizations", response_model=list[uuid.UUID])
async def user_authorizations(
    installation_id: uuid.UUID, body: UserAuthorizationsIn, _: Manage, db: DB
) -> list[uuid.UUID]:
    return await marketplace_service.preapprove_users(get_context(), db, installation_id, body)


@router.get("/processors", response_model=list[ProcessorOut])
async def processors(_: View, db: DB) -> list[ProcessorOut]:
    return await marketplace_service.list_processors(get_context(), db)


@router.get("/data-flows", response_model=list[DataFlowRow])
async def data_flows(
    _: View,
    db: DB,
    installation_id: uuid.UUID | None = None,
    personal_data: bool | None = None,
    destination_country: str | None = None,
) -> list[DataFlowRow]:
    q = DataFlowQuery(
        installation_id=installation_id, personal_data=personal_data, destination_country=destination_country
    )
    return await marketplace_service.data_flows(get_context(), db, q)


@router.get("/policy", response_model=PolicyOut)
async def policy(_: View, db: DB) -> PolicyOut:
    return await marketplace_service.policy_view(get_context(), db)


@router.get("/sdk/manifest-schema")
async def sdk_manifest_schema(_: View) -> dict[str, Any]:
    return marketplace_service.sdk_manifest_schema()


@router.get("/sdk/ports")
async def sdk_ports(_: View) -> dict[str, Any]:
    return marketplace_service.sdk_ports()

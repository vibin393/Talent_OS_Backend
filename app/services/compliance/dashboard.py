# ruff: file-ignore[line-too-long]
"""Compliance dashboard and worker compliance (Epic 21 A7, P2-8.4, FR-116; AD-36 ``worker_compliance``).

The BGV "open cases by age" tile is composed by the frontend from the BGV list API: ``compliance`` never imports
``bgv``.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.compliance import Credential, CredentialType, PlacementComplianceItem
from app.models.placements import Placement
from app.schemas.compliance import (
    ComplianceDashboardOut,
    DashboardCredentialRow,
    DashboardPlacementRow,
    WorkerComplianceStatus,
)
from app.services.compliance import checklist, credentials
from app.services.compliance.common import OPEN_PLACEMENT_STATUSES, today

_clients = table("clients", column("id"), column("name"))


async def dashboard(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    client_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
) -> ComplianceDashboardOut:
    d = await today(db)
    stmt = select(Placement).where(Placement.status.in_(OPEN_PLACEMENT_STATUSES))
    if client_id is not None:
        stmt = stmt.where(Placement.client_id == client_id)
    if legal_entity_id is not None:
        stmt = stmt.where(Placement.legal_entity_id == legal_entity_id)
    placements = list((await db.execute(stmt)).scalars())
    pids = [p.id for p in placements]
    items_by: dict[uuid.UUID, list[PlacementComplianceItem]] = {}
    if pids:
        for it in (
            await db.execute(
                select(PlacementComplianceItem).where(
                    PlacementComplianceItem.placement_id.in_(pids),
                    PlacementComplianceItem.closed_at.is_(None),
                )
            )
        ).scalars():
            items_by.setdefault(it.placement_id, []).append(it)
    compliant = non_compliant = blocking = retro_only = 0
    missing_by_kind: dict[str, int] = {"credential": 0, "document": 0, "bgv_package": 0, "policy_ack": 0}
    rows: list[tuple[Placement, bool, int, int]] = []
    for p in placements:
        items = items_by.get(p.id, [])
        nc, bl = checklist._flags(p, items)
        for it in items:
            if it.mandatory and it.status == "missing":
                missing_by_kind[it.kind] = missing_by_kind.get(it.kind, 0) + 1
        if nc:
            non_compliant += 1
            blocking += int(bl)
            retro_only += int(not bl)
            rows.append(
                (
                    p,
                    bl,
                    sum(1 for i in items if i.mandatory and i.status == "missing"),
                    sum(1 for i in items if i.mandatory and i.status == "expired"),
                )
            )
        else:
            compliant += 1
    rows.sort(key=lambda r: (not r[1], -(r[2] + r[3]), r[0].human_id))
    top = rows[:20]
    names = await credentials.candidate_names(db, [r[0].candidate_id for r in top])
    clients = {}
    cids = list({r[0].client_id for r in top})
    if cids:
        clients = {
            row.id: row.name
            for row in await db.execute(select(_clients.c.id, _clients.c.name).where(_clients.c.id.in_(cids)))
        }
    exp_stmt = (
        select(Credential)
        .where(
            Credential.verification_status == "valid",
            Credential.superseded_at.is_(None),
            Credential.erased_at.is_(None),
            Credential.expires_on.is_not(None),
            Credential.expires_on <= d + timedelta(days=60),
        )
        .order_by(Credential.expires_on)
    )
    if client_id is not None or legal_entity_id is not None:
        exp_stmt = exp_stmt.where(
            Credential.candidate_id.in_([p.candidate_id for p in placements] or [uuid.uuid4()])
        )
    expiring = list((await db.execute(exp_stmt)).scalars())
    types = {t.id: t for t in (await db.execute(select(CredentialType))).scalars()}
    cnames = await credentials.candidate_names(db, [c.candidate_id for c in expiring])

    def left(c: Credential) -> int | None:
        return (c.expires_on - d).days if c.expires_on else None

    return ComplianceDashboardOut(
        compliant=compliant,
        non_compliant=non_compliant,
        blocking=blocking,
        retroactive_only=retro_only,
        expiring_30=sum(1 for c in expiring if (left(c) or 0) >= 0 and (left(c) or 0) <= 30),
        expiring_60=sum(1 for c in expiring if 31 <= (left(c) or 0) <= 60),
        missing_by_kind=missing_by_kind,
        top_non_compliant=[
            DashboardPlacementRow(
                placement_id=p.id,
                human_id=p.human_id,
                candidate_name=names.get(p.candidate_id),
                client_name=clients.get(p.client_id),
                status=p.status,
                blocking=bl,
                missing=m,
                expired=e,
            )
            for p, bl, m, e in top
        ],
        expiring=[
            DashboardCredentialRow(
                credential_id=c.id,
                human_id=c.human_id,
                candidate_id=c.candidate_id,
                candidate_name=cnames.get(c.candidate_id),
                type_label=types[c.credential_type_id].label if c.credential_type_id in types else c.type_key,
                number_last4=c.number_last4,
                expires_on=c.expires_on,
                days_left=left(c),
            )
            for c in expiring[:50]
        ],
    )


async def worker_compliance(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    candidate_id: uuid.UUID,
    on_date: date,
) -> WorkerComplianceStatus:
    """AD-36 contract: the Worker is compliant on ``on_date`` unless a mandatory credential of an open
    placement's checklist has no unexpired valid version (waived items excepted)."""
    keys = set(
        (
            await db.execute(
                select(PlacementComplianceItem.ref)
                .join(Placement, Placement.id == PlacementComplianceItem.placement_id)
                .where(
                    Placement.candidate_id == candidate_id,
                    Placement.status.in_(OPEN_PLACEMENT_STATUSES),
                    PlacementComplianceItem.kind == "credential",
                    PlacementComplianceItem.mandatory.is_(True),
                    PlacementComplianceItem.closed_at.is_(None),
                    PlacementComplianceItem.status != "waived",
                )
            )
        ).scalars()
    )
    expired: list[str] = []
    for key in sorted(keys):
        status, _ = credentials.best_of(
            await credentials.current_versions(db, candidate_id, key), on_date, {}
        )
        if status == "expired":
            expired.append(key)
    return WorkerComplianceStatus(candidate_id=candidate_id, compliant=not expired, expired_mandatory=expired)

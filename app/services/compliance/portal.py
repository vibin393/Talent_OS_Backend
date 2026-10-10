# ruff: noqa: E501
"""Worker "My Work" credentials (Epic 21 A7). Runs in the worker org scope (credentials / checklist rows of other
Workers are invisible) and checks ownership explicitly (another Worker's credential is a 404). The output omits
verifier ids, rejection-note internals and requirement ids."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.compliance import Credential, CredentialType, PlacementComplianceItem
from app.models.placements import Placement
from app.schemas.compliance import CredentialIn, RenewIn
from app.schemas.compliance_portal import (
    PortalCredentialOut,
    PortalCredentialsOut,
    PortalCredentialTypeOut,
    PortalNeededOut,
)
from app.services.compliance import credentials
from app.services.compliance.common import OPEN_PLACEMENT_STATUSES, not_found


def _out(c: Credential, t: CredentialType | None) -> PortalCredentialOut:
    status = credentials.effective_status(c)
    return PortalCredentialOut(
        id=c.id,
        type_key=c.type_key,
        type_label=t.label if t else c.type_key,
        number_last4=c.number_last4,
        issued_on=c.issued_on,
        expires_on=c.expires_on,
        status=status,
        needs_reupload=c.redaction_status == "rejected",
        has_document=c.document_id is not None,
        version_no=c.version_no,
    )


async def list_mine(db: AsyncSession, candidate_id: uuid.UUID) -> PortalCredentialsOut:
    types = {t.id: t for t in (await db.execute(select(CredentialType))).scalars()}
    creds = list(
        (
            await db.execute(
                select(Credential)
                .where(
                    Credential.candidate_id == candidate_id,
                    Credential.superseded_at.is_(None),
                    Credential.erased_at.is_(None),
                )
                .order_by(Credential.type_key, Credential.version_no.desc())
            )
        ).scalars()
    )
    items = list(
        (
            await db.execute(
                select(PlacementComplianceItem)
                .join(Placement, Placement.id == PlacementComplianceItem.placement_id)
                .where(
                    Placement.candidate_id == candidate_id,
                    Placement.status.in_(OPEN_PLACEMENT_STATUSES),
                    PlacementComplianceItem.closed_at.is_(None),
                    PlacementComplianceItem.kind.in_(("credential", "document")),
                    PlacementComplianceItem.mandatory.is_(True),
                )
            )
        ).scalars()
    )
    needed: dict[tuple[str, str], PortalNeededOut] = {}
    for it in items:
        if it.status in ("met", "waived"):
            continue
        needed[it.kind, it.ref] = PortalNeededOut(
            label=it.label,
            kind=it.kind,
            type_key=it.ref if it.kind == "credential" else None,
            status=it.status,
        )
    outs = [_out(c, types.get(c.credential_type_id)) for c in creds]
    return PortalCredentialsOut(
        credentials=outs,
        needed=sorted(needed.values(), key=lambda n: n.label),
        types=[
            PortalCredentialTypeOut(
                key=t.key,
                label=t.label,
                category=t.category,
                has_expiry=t.has_expiry,
                number_kind=t.number_kind,
                requires_document=t.requires_document,
            )
            for t in sorted(types.values(), key=lambda t: t.label)
            if t.archived_at is None
        ],
        expiring_soon=sum(1 for o in outs if o.status in ("expiring_soon", "expired")),
    )


async def _own(db: AsyncSession, candidate_id: uuid.UUID, credential_id: uuid.UUID) -> Credential:
    c = await db.get(Credential, credential_id)
    if c is None or c.candidate_id != candidate_id or c.erased_at is not None:
        raise not_found("Credential")
    return c


async def record_mine(
    db: AsyncSession, candidate_id: uuid.UUID, data: CredentialIn, file: tuple[bytes, str | None] | None
) -> PortalCredentialOut:
    ctx = RequestContext(actor_kind="candidate")
    c = await credentials.record(ctx, db, candidate_id, data, file, source="worker_portal")
    return _out(c, await db.get(CredentialType, c.credential_type_id))


async def renew_mine(
    db: AsyncSession,
    candidate_id: uuid.UUID,
    credential_id: uuid.UUID,
    data: RenewIn,
    file: tuple[bytes, str | None] | None,
) -> PortalCredentialOut:
    await _own(db, candidate_id, credential_id)
    ctx = RequestContext(actor_kind="candidate")
    c = await credentials.renew(
        ctx, db, credential_id, data, file, source="worker_portal", candidate_id=candidate_id
    )
    return _out(c, await db.get(CredentialType, c.credential_type_id))


async def my_document(
    db: AsyncSession, candidate_id: uuid.UUID, credential_id: uuid.UUID
) -> tuple[bytes, str, str]:
    c = await _own(db, candidate_id, credential_id)
    return await credentials.document_bytes(RequestContext(actor_kind="candidate"), db, c, worker=True)

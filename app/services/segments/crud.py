# ruff: noqa: E501
"""Segment CRUD, versions and preview (Epic 38 story 38.1, FR-192).

* Reading needs ``pool:read``; a segment is visible to its owner, to members of its team (``team``), to everyone
  (``tenant``) and to ``pool:manage`` holders.
* Writing needs ``segment:write``; only the owner (or ``pool:manage``) edits. ``visibility='tenant'`` needs
  ``pool:manage``.
* Each save that changes the definition appends a ``segment_versions`` row and bumps ``definition_version``.
* Definitions are validated by ``core.semantic.validate(defn, principal=<author>)``; uuid values that are
  candidate ids are refused (segments hold no person data, A10).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import Principal, forbidden
from app.core.semantic import Definition
from app.models.candidates import Candidate
from app.models.talent_pools import Segment, SegmentVersion
from app.schemas.segments import (
    SegmentCreate,
    SegmentOut,
    SegmentPreviewIn,
    SegmentPreviewOut,
    SegmentUpdate,
    SegmentVersionOut,
)
from app.services.segments import definitions, service

READ = "pool:read"
WRITE = "segment:write"
MANAGE = "pool:manage"
PREVIEW_ROWS = 50


def now() -> datetime:
    return datetime.now(UTC)


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        raise NotFound("Segment not found")
    return principal


def visible(principal: Principal) -> ColumnElement[bool]:
    if principal.has(MANAGE):
        return true()
    return or_(
        Segment.owner_id == principal.id,
        Segment.visibility == "tenant",
        Segment.team_id.in_(list(principal.team_ids)) if principal.team_ids else false(),
    )


def out(row: Segment) -> SegmentOut:
    return SegmentOut(
        id=row.id,
        name=row.name,
        description=row.description,
        source_key=row.source_key,
        definition=dict(row.definition or {}),
        definition_version=row.definition_version,
        owner_id=row.owner_id,
        visibility=row.visibility,
        team_id=row.team_id,
        needs_repair=row.needs_repair,
        repair_issues=list(row.repair_issues or []),
        archived_at=row.archived_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def get_visible(ctx: RequestContext, db: AsyncSession, segment_id: uuid.UUID) -> Segment:
    principal = principal_of(ctx)
    row = (
        await db.execute(select(Segment).where(Segment.id == segment_id, visible(principal)))
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Segment not found")
    return row


def _check_visibility(principal: Principal, visibility: str, team_id: uuid.UUID | None) -> uuid.UUID | None:
    if visibility == "tenant" and not principal.has(MANAGE):
        raise forbidden(MANAGE)
    if visibility == "team":
        if team_id is None and principal.team_ids:
            team_id = sorted(principal.team_ids)[0]  # the caller's team when none is named
        if team_id is None:
            raise ValidationFailed(
                "Choose the team that can see it", code="team_required", details={"path": "team_id"}
            )
        if team_id not in principal.team_ids and not principal.has(MANAGE):
            raise ValidationFailed(
                "You can share only with your own teams", code="team_not_member", details={"path": "team_id"}
            )
    return team_id


async def checked_definition(
    db: AsyncSession, principal: Principal, source_key: str, raw: dict[str, Any]
) -> Definition:
    defn = definitions.build(source_key, raw, principal=principal)
    values = definitions.uuid_values(defn)
    if values:
        hit = (await db.execute(select(Candidate.id).where(Candidate.id.in_(list(values))).limit(1))).first()
        if hit is not None:
            raise ValidationFailed(
                "A segment cannot name an individual candidate",
                code="segment_person_reference",
                details={"path": "filters"},
            )
    return defn


async def list_segments(
    ctx: RequestContext, db: AsyncSession, *, include_archived: bool = False
) -> list[SegmentOut]:
    principal = principal_of(ctx)
    stmt = select(Segment).where(visible(principal))
    if not include_archived:
        stmt = stmt.where(Segment.archived_at.is_(None))
    rows = (await db.execute(stmt.order_by(Segment.name, Segment.id).limit(1000))).scalars()
    return [out(r) for r in rows]


async def create_segment(
    ctx: RequestContext, db: AsyncSession, body: SegmentCreate, *, owner_visibility_checked: bool = False
) -> Segment:
    principal = principal_of(ctx)
    if not owner_visibility_checked:
        body = body.model_copy(update={"team_id": _check_visibility(principal, body.visibility, body.team_id)})
    defn = await checked_definition(db, principal, body.source_key, body.definition)
    row = Segment(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        name=body.name,
        description=body.description,
        source_key=body.source_key,
        definition=definitions.stored(defn),
        definition_version=1,
        owner_id=principal.id,
        visibility=body.visibility,
        team_id=body.team_id if body.visibility == "team" else None,
    )
    db.add(row)
    await db.flush()
    db.add(
        SegmentVersion(
            tenant_id=ctx.tenant_id,
            segment_id=row.id,
            version=1,
            definition=row.definition,
            created_by=principal.id,
        )
    )
    await db.flush()
    await db.refresh(row)
    return row


def can_edit(principal: Principal, row: Segment) -> bool:
    return principal.has(MANAGE) or (row.owner_id == principal.id and principal.has(WRITE))


async def update_segment(
    ctx: RequestContext, db: AsyncSession, segment_id: uuid.UUID, body: SegmentUpdate
) -> Segment:
    principal = principal_of(ctx)
    row = await get_visible(ctx, db, segment_id)
    if not can_edit(principal, row):
        raise forbidden(WRITE)
    sent = body.model_fields_set
    if "name" in sent and body.name:
        row.name = body.name
    if "description" in sent:
        row.description = body.description
    if "visibility" in sent and body.visibility:
        team = body.team_id if "team_id" in sent else row.team_id
        team = _check_visibility(principal, body.visibility, team)
        row.visibility = body.visibility
        row.team_id = team if body.visibility == "team" else None
    if "definition" in sent and body.definition is not None:
        defn = await checked_definition(db, principal, row.source_key, body.definition)
        new_json = definitions.stored(defn)
        if new_json != row.definition:
            row.definition = new_json
            row.definition_version += 1
            row.needs_repair = False
            row.repair_issues = []
            db.add(
                SegmentVersion(
                    tenant_id=ctx.tenant_id,
                    segment_id=row.id,
                    version=row.definition_version,
                    definition=new_json,
                    created_by=principal.id,
                )
            )
    await db.flush()
    await db.refresh(row)
    return row


async def archive_segment(ctx: RequestContext, db: AsyncSession, segment_id: uuid.UUID) -> Segment:
    principal = principal_of(ctx)
    row = await get_visible(ctx, db, segment_id)
    if not can_edit(principal, row):
        raise forbidden(WRITE)
    if row.archived_at is None:
        row.archived_at = now()
        await db.flush()
    await db.refresh(row)
    return row


async def versions(ctx: RequestContext, db: AsyncSession, segment_id: uuid.UUID) -> list[SegmentVersionOut]:
    row = await get_visible(ctx, db, segment_id)
    found = (
        await db.execute(
            select(SegmentVersion)
            .where(SegmentVersion.segment_id == row.id)
            .order_by(SegmentVersion.version.desc())
        )
    ).scalars()
    return [
        SegmentVersionOut(
            version=v.version, definition=dict(v.definition), created_by=v.created_by, created_at=v.created_at
        )
        for v in found
    ]


async def preview(ctx: RequestContext, db: AsyncSession, body: SegmentPreviewIn) -> SegmentPreviewOut:
    """Read-only preview (foundation savepoint): first 50 rows + count, with the caller's scope."""
    principal = principal_of(ctx)
    defn = definitions.build(body.source_key, body.definition, principal=principal)
    result = await service.preview_result(service.semantic_context(ctx, db), defn, PREVIEW_ROWS)
    rows = [{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items()} for r in result.rows]
    return SegmentPreviewOut(
        count=int(result.total or 0), columns=result.columns, rows=rows, masked_fields=result.masked_fields
    )

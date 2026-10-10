# ruff: noqa: D401
"""Report visibility and sharing (Epic 47 story 47.3, FR-238, NFR-33).

* The owner always sees the report. Anyone else needs ``report:read`` AND a ``report_shares`` row matching
  ``tenant``, one of their teams or one of their roles.
* ``PUT /reports/custom/{id}/shares`` (owner, ``report:build``) replaces the share list. ``tenant`` / ``role``
  targets need ``report:share_tenant`` (403). A Role share is refused (422 ``share_role_lacks_permission``)
  when the Role grants the source's read permission at no scope. Team shares are always allowed: each viewer
  is checked at run time. Sharing never elevates -- every run executes as the viewer.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import Principal, check, effective_role_grants
from app.core.semantic import get_source
from app.models.identity import Role, Team
from app.models.report_builder import ReportDefinition, ReportShare
from app.schemas.report_builder import ShareIn, ShareOut
from app.services.reporting.builder.common import BUILD, READ, SHARE_TENANT, principal_of

__all__ = ["can_see", "put_shares", "shares_out", "source_permission", "visible_condition", "visible_report"]


def visible_condition(principal: Principal) -> ColumnElement[bool]:
    """SQL: reports ``principal`` may open (owner, or shared with them and holding ``report:read``)."""
    own = ReportDefinition.owner_id == principal.id
    if not principal.has(READ):
        return own
    targets: list[ColumnElement[bool]] = [ReportShare.target_kind == "tenant"]
    if principal.team_ids:
        targets.append(and_(ReportShare.target_kind == "team", ReportShare.team_id.in_(principal.team_ids)))
    if principal.role_ids:
        targets.append(and_(ReportShare.target_kind == "role", ReportShare.role_id.in_(principal.role_ids)))
    shared = exists(select(ReportShare.id).where(ReportShare.report_id == ReportDefinition.id, or_(*targets)))
    return or_(own, shared)


async def can_see(db: AsyncSession, principal: Principal, report_id: uuid.UUID) -> bool:
    stmt = select(ReportDefinition.id).where(
        ReportDefinition.id == report_id, ReportDefinition.archived_at.is_(None), visible_condition(principal)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def visible_report(
    db: AsyncSession, principal: Principal, report_id: uuid.UUID, *, for_update: bool = False
) -> ReportDefinition:
    """The report when ``principal`` may see it, else 404 (never reveals existence)."""
    stmt = select(ReportDefinition).where(
        ReportDefinition.id == report_id, ReportDefinition.archived_at.is_(None), visible_condition(principal)
    )
    if for_update:
        stmt = stmt.with_for_update(of=ReportDefinition)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Report not found", code="report_not_found")
    return row


def source_permission(source_key: str) -> str | None:
    try:
        return get_source(source_key).scope.permission
    except KeyError:
        return None


async def shares_out(db: AsyncSession, report_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[ShareOut]]:
    if not report_ids:
        return {}
    rows = (
        await db.execute(
            select(ReportShare, Team.name, Role.name)
            .outerjoin(Team, Team.id == ReportShare.team_id)
            .outerjoin(Role, Role.id == ReportShare.role_id)
            .where(ReportShare.report_id.in_(report_ids))
            .order_by(ReportShare.target_kind, ReportShare.created_at)
        )
    ).all()
    out: dict[uuid.UUID, list[ShareOut]] = {rid: [] for rid in report_ids}
    for share, team_name, role_name in rows:
        name = "Everyone" if share.target_kind == "tenant" else (team_name or role_name)
        out[share.report_id].append(
            ShareOut(target_kind=share.target_kind, team_id=share.team_id, role_id=share.role_id, name=name)
        )
    return out


def _normalise(items: Sequence[ShareIn]) -> list[ShareIn]:
    seen: set[tuple[Any, ...]] = set()
    out: list[ShareIn] = []
    for i, s in enumerate(items):
        if s.target_kind == "team" and (s.team_id is None or s.role_id is not None):
            raise ValidationFailed(
                "A team share needs team_id only", code="share_target_invalid", details={"path": f"[{i}]"}
            )
        if s.target_kind == "role" and (s.role_id is None or s.team_id is not None):
            raise ValidationFailed(
                "A role share needs role_id only", code="share_target_invalid", details={"path": f"[{i}]"}
            )
        if s.target_kind == "tenant" and (s.team_id is not None or s.role_id is not None):
            raise ValidationFailed(
                "A tenant share has no target id", code="share_target_invalid", details={"path": f"[{i}]"}
            )
        key = (s.target_kind, s.team_id, s.role_id)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


async def put_shares(
    ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID, items: Sequence[ShareIn]
) -> list[ShareOut]:
    principal = principal_of(ctx)
    check(principal, BUILD)
    report = await visible_report(db, principal, report_id, for_update=True)
    if report.owner_id != principal.id:
        raise NotFound("Report not found", code="report_not_found")
    shares = _normalise(items)
    if any(s.target_kind in {"tenant", "role"} for s in shares):
        check(principal, SHARE_TENANT)
    read_permission = source_permission(report.source_key)
    for i, s in enumerate(shares):
        if s.target_kind == "team":
            if (await db.get(Team, s.team_id)) is None:
                raise ValidationFailed(
                    "Unknown team", code="share_target_invalid", details={"path": f"[{i}].team_id"}
                )
        elif s.target_kind == "role":
            role = await db.get(Role, s.role_id)
            if role is None:
                raise ValidationFailed(
                    "Unknown role", code="share_target_invalid", details={"path": f"[{i}].role_id"}
                )
            grants = effective_role_grants(role.key, role.is_system, role.permissions or {})
            if read_permission is None or read_permission not in grants or READ not in grants:
                raise ValidationFailed(
                    f"The role {role.name!r} cannot read this report's data ({read_permission})",
                    code="share_role_lacks_permission",
                    details={"path": f"[{i}].role_id", "required_permission": read_permission},
                )
    await db.execute(delete(ReportShare).where(ReportShare.report_id == report.id))
    for s in shares:
        db.add(
            ReportShare(
                tenant_id=report.tenant_id,
                report_id=report.id,
                target_kind=s.target_kind,
                team_id=s.team_id,
                role_id=s.role_id,
                created_by=principal.id,
            )
        )
    report.visibility = "shared" if shares else "private"
    await db.flush()
    audit.record(
        "report.shares_changed",
        "report_definition",
        report.id,
        session=db,
        report_id=str(report.id),
        version=report.definition_version,
        shares=[{"target_kind": s.target_kind, "team_id": str(s.team_id) if s.team_id else None,
                 "role_id": str(s.role_id) if s.role_id else None} for s in shares],
    )  # fmt: skip
    return (await shares_out(db, [report.id]))[report.id]

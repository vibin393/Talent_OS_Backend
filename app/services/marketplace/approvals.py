# ruff: noqa: E501, ARG001
"""Sensitive-install approvals (Epic 69, spec A4 step 7 / A5): the ``installation_grant`` approval subject on the P2
engine (human approvers only -- the engine refuses every automated actor). The chain is the one named by
``app_policy.sensitive_approval_chain_id``; its steps are snapshotted as explicit steps.

Approve -> connectors run the connection test (pass -> ``active``, fail -> ``pending_consent``); Apps -> ``active``.
Reject -> ``uninstalled`` (``uninstalled_by`` NULL, ``suspended_reason`` "Approval rejected: <reason>"), secrets deleted,
configs disabled.
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.events import emit
from app.events.ecosystem import InstallationChanged
from app.models.approvals import ApprovalChain, ApprovalChainStep
from app.models.connectors import ProviderConnectorConfig
from app.models.integrations import ConnectorSyncState
from app.models.oauth import AppGrant, AppInstallation
from app.schemas.approvals import SubjectSummary
from app.services import vault_service
from app.services.marketplace import health

SUBJECT = "installation_grant"


async def _steps(db: AsyncSession, chain_id: uuid.UUID) -> list[Any]:
    from app.services.approvals import StepSpec

    chain = await db.get(ApprovalChain, chain_id)
    if chain is None or not chain.is_active:
        raise ValidationFailed(
            "The approval chain in your app policy is not available",
            code="approval_chain_unavailable",
            details={"fields": {"sensitive_approval_chain_id": "Not found"}},
        )
    rows = (
        await db.execute(
            select(ApprovalChainStep)
            .where(ApprovalChainStep.chain_id == chain_id)
            .order_by(ApprovalChainStep.position)
        )
    ).scalars()
    out: list[Any] = []
    for s in rows:
        if s.approver_kind == "user" and s.approver_user_id:
            out.append(StepSpec(s.name, "user", {"user_id": str(s.approver_user_id)}, s.condition))
        elif s.approver_kind == "role" and s.approver_role_id:
            out.append(StepSpec(s.name, "role", {"role_id": str(s.approver_role_id)}, s.condition))
        else:
            out.append(StepSpec(s.name, s.approver_kind, dict(s.approver_ref or {}), s.condition))
    return out


async def request(
    ctx: RequestContext,
    db: AsyncSession,
    inst: AppInstallation,
    *,
    chain_id: uuid.UUID,
    listing_name: str,
    sensitive: list[str],
) -> tuple[str, uuid.UUID | None]:
    """Start the approval; returns (``pending`` | ``auto_approved``, request id)."""
    from app.services import approvals

    steps = await _steps(db, chain_id)
    facts = {
        "listing_name": listing_name,
        "sensitive_scopes": sorted(sensitive),
        "purposes": {k: v for k, v in dict(inst.sensitive_purposes or {}).items() if k in sensitive},
    }
    result = await approvals.start(ctx, db, SUBJECT, inst.id, facts, steps=steps)
    if result.request_id is not None:
        await db.execute(
            update(AppGrant)
            .where(AppGrant.installation_id == inst.id, AppGrant.superseded_at.is_(None))
            .values(approval_id=result.request_id)
        )
    return result.status, result.request_id


async def pending_request_id(db: AsyncSession, installation_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.approvals import ApprovalRequest

    return (
        await db.execute(
            select(ApprovalRequest.id).where(
                ApprovalRequest.subject_type == SUBJECT,
                ApprovalRequest.subject_id == installation_id,
                ApprovalRequest.status == "pending",
            )
        )
    ).scalar_one_or_none()


async def _approved(ctx: RequestContext, db: AsyncSession, inst: AppInstallation) -> None:
    if inst.status != "pending_approval":
        return
    if inst.kind == "connector":
        inst.status = "pending_consent"
        outcome = await health.run_test(ctx, db, inst, trigger="install")
        if outcome.ok:
            inst.status = "active"
    else:
        inst.status = "active"
    audit.record("marketplace.approved", "app_installation", inst.id, session=db, status=inst.status)
    emit(db, InstallationChanged(
        tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=inst.app_id,
        connector_key=inst.connector_key, change="granted", status=inst.status, actor_kind="user",
    ))  # fmt: skip


async def _rejected(
    ctx: RequestContext, db: AsyncSession, inst: AppInstallation, comment: str | None
) -> None:
    if inst.status != "pending_approval":
        return
    config_ids = list(
        (
            await db.execute(
                select(ProviderConnectorConfig.id).where(ProviderConnectorConfig.installation_id == inst.id)
            )
        ).scalars()
    )
    for cid in config_ids:
        await vault_service.delete_owner(ctx, db, "provider_config", cid)
    await vault_service.delete_owner(ctx, db, "installation", inst.id)
    await db.execute(
        update(ProviderConnectorConfig)
        .where(ProviderConnectorConfig.installation_id == inst.id)
        .values(status="disabled")
    )
    await db.execute(
        update(ConnectorSyncState).where(ConnectorSyncState.installation_id == inst.id).values(enabled=False)
    )
    inst.status, inst.uninstalled_by = "uninstalled", None
    from datetime import UTC, datetime

    inst.uninstalled_at = datetime.now(UTC)
    inst.suspended_reason = f"Approval rejected: {(comment or '').strip()}"[:500]
    audit.record("marketplace.approval_rejected", "app_installation", inst.id, session=db)
    emit(db, InstallationChanged(
        tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=inst.app_id,
        connector_key=inst.connector_key, change="uninstalled", status="uninstalled", actor_kind="user",
    ))  # fmt: skip


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    inst = (
        await db.execute(
            select(AppInstallation).where(AppInstallation.id == decision.subject_id).with_for_update()
        )
    ).scalar_one_or_none()
    if inst is None:
        return
    if decision.outcome in ("approved", "auto_approved"):
        await _approved(ctx, db, inst)
    else:
        await _rejected(ctx, db, inst, decision.comment)
    await db.flush()


async def describe(
    ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, SubjectSummary]:
    rows = (
        await db.execute(
            select(AppInstallation.id, AppInstallation.display_name).where(AppInstallation.id.in_(ids))
        )
    ).all()
    return {r[0]: SubjectSummary(label="Integration install", title=f"Install {r[1]}") for r in rows}


def register() -> None:
    from app.services.approvals import register_approval_subject
    from app.services.approvals.registry import subject

    if subject(SUBJECT) is None:
        register_approval_subject(
            SUBJECT, on_final_decided=on_final_decided, describe=describe, permission="integration:manage",
            auto_approve_calls_back=False,
        )  # fmt: skip


__all__ = ["SUBJECT", "describe", "on_final_decided", "pending_request_id", "register", "request"]

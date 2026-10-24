"""Registrations of the ``expenses`` domain (autodiscovered by ``core.hooks``).

* Approval subject ``expense_claim`` (callbacks synchronous in the deciding transaction, AD-24); the route is
  the Timesheets ``placement_time_route`` resolved through ``approvals.resolve_route``.
* Posting source ``expense_line`` (AD-23, pull-only, both ledgers).
* Notification type ``expenses.claim_decided`` (Worker); workflow snapshot loader ``expense_claim`` (trigger
  ``expenses.claim_submitted`` is catalogued by the foundation); SLA milestone ``(expense_claim,
  step_decision)``; offboarding verifier ``final_expense_settlement``; file access checker
  ``expense_receipt`` (internal ``expense:read``).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.approvals import register_approval_subject


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- approvals + postings
def _register_core() -> None:
    from app.services.expenses import approval, posting_source

    register_approval_subject(
        "expense_claim",
        on_final_decided=approval.on_final_decided,
        on_step_decided=approval.on_step_decided,
        describe=approval.describe,
        permission="expense:approve",
    )
    posting_source.register()


_register_core()


# ---------------------------------------------------------------- notifications
async def _claim(db: AsyncSession, payload: Any) -> Any:
    from app.models.expenses import ExpenseClaim

    cid = _uuid(payload.get("claim_id"))
    if cid is None:
        return None
    return (await db.execute(select(ExpenseClaim).where(ExpenseClaim.id == cid))).scalar_one_or_none()


async def _worker(db: AsyncSession, payload: Any) -> set[Any]:
    from app.services.notifications_service import Recipient

    claim = await _claim(db, payload)
    return {Recipient("candidate", claim.candidate_id)} if claim is not None else set()


async def _render_decided(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    claim = await _claim(db, payload)
    label = f"Expense claim {claim.human_id}" if claim is not None else "Your expense claim"
    outcome = str(payload.get("outcome") or "decided").replace("_", " ")
    if claim is None:
        return Content(title=f"{label} was {outcome}"[:300])
    return Content(
        title=f"{label} was {outcome}"[:300],
        body=claim.rejection_comment if outcome == "rejected" else None,
        entity_type="expense_claim",
        entity_id=claim.id,
        link_path=f"/portal/work/expenses/{claim.id}",
    )


def _register_notifications() -> None:
    from app.events.expenses import ExpenseClaimDecided
    from app.services.notifications_service import (
        NotificationType,
        register_event_source,
        register_notification_type,
    )

    try:
        register_notification_type(
            "expenses.claim_decided",
            NotificationType("Expense claim decided", "Expenses", "both", True, _worker, _render_decided),
        )
    except Exception:
        logger.debug("notification type expenses.claim_decided already registered")
        return
    register_event_source(ExpenseClaimDecided, "expenses.claim_decided")


try:
    _register_notifications()
except Exception:  # pragma: no cover - never block other registrations
    logger.exception("expenses notification registrations failed")


# ---------------------------------------------------------------- workflow, SLA, offboarding, files
async def _snapshot(ctx: RequestContext, db: AsyncSession, claim_id: uuid.UUID) -> dict[str, Any] | None:  # ruff: ignore[unused-function-argument]
    from app.models.expenses import ExpenseClaim

    claim = (await db.execute(select(ExpenseClaim).where(ExpenseClaim.id == claim_id))).scalar_one_or_none()
    if claim is None:
        return None
    return {
        "id": str(claim.id),
        "status": claim.status,
        "placement_id": str(claim.placement_id),
        "client_id": str(claim.client_id),
        "legal_entity_id": str(claim.legal_entity_id),
        "currency": claim.currency,
        "total_claimed": str(claim.total_claimed),
    }


async def _receipt_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:  # ruff: ignore[unused-function-argument]  # NOSONAR
    principal = ctx.principal
    return bool(
        not write and principal is not None and hasattr(principal, "has") and principal.has("expense:read")
    )


def _register_platform() -> None:
    from app.services import files_service
    from app.services.expenses.service import final_expense_settlement
    from app.services.placements_service import offboarding_verifiers, register_offboarding_verifier
    from app.services.sla.registry import SlaMilestoneDef, milestone, register_sla_milestone
    from app.services.workflow import catalogue
    from app.services.workflow.conditions import FieldDef

    if catalogue.snapshot_loader("expense_claim") is None:
        catalogue.register_snapshot_loader(
            "expense_claim",
            _snapshot,
            {
                "status": FieldDef(
                    "enum",
                    "Status",
                    (
                        "draft",
                        "submitted",
                        "in_approval",
                        "approved",
                        "partially_approved",
                        "rejected",
                        "reimbursed",
                        "stated",
                    ),
                ),
                "placement_id": FieldDef("uuid", "Placement"),
                "client_id": FieldDef("uuid", "Client"),
                "legal_entity_id": FieldDef("uuid", "Legal entity"),
                "currency": FieldDef("str", "Currency"),
                "total_claimed": FieldDef("decimal", "Total claimed"),
            },
        )
    if milestone("expense_claim", "step_decision") is None:
        register_sla_milestone(
            "expense_claim",
            "step_decision",
            SlaMilestoneDef("Expense claim approval step decision", scope_keys=("route_step",)),
        )
    if "final_expense_settlement" not in offboarding_verifiers():
        register_offboarding_verifier("final_expense_settlement", final_expense_settlement)  # type: ignore[arg-type]
    files_service.register_access_checker("expense_receipt", _receipt_access)


try:
    _register_platform()
except Exception:  # pragma: no cover - never block other domains' registrations
    logger.exception("expenses platform registrations failed")

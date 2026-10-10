# ruff: noqa: E501
"""BGV case status machine (PRD §5.6, AD-4). ``transition_case`` is the only writer of ``bgv_cases.status``.

* Automation moves (``documents_pending`` -> ``in_verification`` -> ``discrepancy`` / ``under_review``) are made
  by the **system** only (``recompute_case_status`` runs after every check or discrepancy change).
* ★ ``-> clear`` / ``-> failed`` (adjudication) are human-only: ``ai``, ``workflow`` and ``system`` are refused with
  409 ``transition_not_allowed`` (FR-181). ``recompute`` never sets them.
* ``-> cancelled``: a user (Compliance, with a reason) or the system (dropped placement).
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.errors import ConflictError
from app.core.events import emit
from app.events.bgv import BgvCaseStatusChanged
from app.models.bgv import CASE_TERMINAL, CHECK_FINAL, BgvCase, BgvCheck, BgvDiscrepancy, BgvDocumentRequest
from app.services.bgv.common import consent_active, record_event

_SYSTEM = frozenset({"system"})
_USER = frozenset({"user"})
_CANCEL = frozenset({"user", "system"})
_AUTO = ("documents_pending", "in_verification", "discrepancy", "under_review")

TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("initiated", "documents_pending"): _SYSTEM,
    ("documents_pending", "in_verification"): _SYSTEM,
    ("in_verification", "discrepancy"): _SYSTEM,
    ("in_verification", "under_review"): _SYSTEM,
    ("discrepancy", "in_verification"): _SYSTEM,
    ("discrepancy", "under_review"): _SYSTEM,
    ("under_review", "in_verification"): _SYSTEM,
    ("under_review", "discrepancy"): _SYSTEM,
    # ★ adjudication (human-only)
    ("under_review", "clear"): _USER,
    ("under_review", "failed"): _USER,
    ("discrepancy", "failed"): _USER,
}
for _s in ("initiated", *_AUTO):
    TRANSITIONS[_s, "cancelled"] = _CANCEL
HUMAN_ONLY = frozenset(k for k in TRANSITIONS if k[1] in ("clear", "failed"))


def not_allowed(from_status: str, to_status: str, actor_kind: str) -> ConflictError:
    return ConflictError(
        f"A BGV case cannot move from {from_status} to {to_status}",
        code="transition_not_allowed",
        details={"from": from_status, "to": to_status, "actor": actor_kind},
    )


def check_transition(from_status: str, to_status: str, actor_kind: str) -> None:
    allowed = TRANSITIONS.get((from_status, to_status))
    if allowed is None or actor_kind not in allowed:
        raise not_allowed(from_status, to_status, actor_kind)


async def transition_case(
    db: AsyncSession, case: BgvCase, to: str, who: ActorRef, *, reason: str | None = None
) -> BgvCase:
    from_status = case.status
    check_transition(from_status, to, who.kind)
    case.status = to
    case.lock_version = (case.lock_version or 1) + 1
    record_event(db, case, "status", who, from_status=from_status, to_status=to, reason=reason)
    await db.flush()
    emit(
        db,
        BgvCaseStatusChanged(
            tenant_id=case.tenant_id,
            case_id=case.id,
            candidate_id=case.candidate_id,
            placement_id=case.placement_id,
            from_status=from_status,
            to_status=to,
        ),
    )
    return case


async def documents_complete(db: AsyncSession, case: BgvCase) -> bool:
    rows = (
        await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == case.id))
    ).scalars()
    return all(r.status in ("submitted", "accepted") for r in rows if r.required)


def target_status(checks: list[BgvCheck], discrepancies: list[BgvDiscrepancy]) -> str:
    awaiting = any(d.status == "awaiting_response" for d in discrepancies)
    open_ids = {d.check_id for d in discrepancies if d.status != "resolved"}
    all_final = bool(checks) and all(c.status in CHECK_FINAL for c in checks)
    if all_final and not awaiting:
        return "under_review"
    if any(c.status == "discrepancy" and c.id in open_ids for c in checks):
        return "discrepancy"
    return "in_verification"


async def recompute_case_status(db: AsyncSession, case: BgvCase) -> str:
    """System automation after every check / discrepancy / document change. Never sets clear / failed."""
    if case.status in CASE_TERMINAL or case.status == "initiated":
        return case.status
    system = ActorRef("system", None)
    checks = list((await db.execute(select(BgvCheck).where(BgvCheck.case_id == case.id))).scalars())
    if case.status == "documents_pending":
        started = any(c.status != "pending" for c in checks)
        if not (started and await consent_active(db, case) and await documents_complete(db, case)):
            return case.status
        await transition_case(db, case, "in_verification", system)
    discrepancies = list(
        (await db.execute(select(BgvDiscrepancy).where(BgvDiscrepancy.case_id == case.id))).scalars()
    )
    target = target_status(checks, discrepancies)
    if target != case.status:
        await transition_case(db, case, target, system)
    return case.status


def describe() -> list[dict[str, Any]]:
    return [
        {"from": a, "to": b, "actors": sorted(k), "human_only": (a, b) in HUMAN_ONLY}
        for (a, b), k in TRANSITIONS.items()
    ]

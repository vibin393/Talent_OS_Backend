# ruff: file-ignore[line-too-long]
"""Invitation lifecycle (Epic 40 A5, spec section 5.1): ``TRANSITIONS[(from, to)] = allowed actor kinds``.

Anything else -> 409 ``transition_not_allowed``. Gating Pass / Fail is human only (FR-257, marked with a star):
``system`` may move ``reviewed -> passed`` only for a NON-gating invitation when the tenant allows auto-pass and the
score is at or above the assessment's ``auto_pass_threshold_pct``. ``recorded_by_user`` walks a Manual provider
result through ``invited -> started -> completed -> reviewed``; ``system`` may start a provider invitation when the
provider reports a result. This module never reads proctoring signals (invariant, A8).
"""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.events.assessments import AssessmentInvitationDecided
from app.models.assessments import AssessmentInvitation
from app.services.assessments.common import conflict, now
from app.services.assessments.events import AssessmentInvitationStatusChanged

HUMAN_DECIDERS = frozenset({"user", "recorded_by_user"})
TRANSITIONS: dict[tuple[str | None, str], frozenset[str]] = {
    (None, "invited"): frozenset({"user", "workflow"}),
    ("invited", "started"): frozenset({"candidate", "secure_link", "recorded_by_user"}),
    ("invited", "expired"): frozenset({"system"}),
    ("expired", "invited"): frozenset({"user"}),
    ("started", "completed"): frozenset({"candidate", "secure_link", "system", "recorded_by_user"}),
    ("completed", "reviewed"): frozenset({"system", "user", "recorded_by_user"}),
    ("reviewed", "passed"): frozenset({"user", "recorded_by_user", "system"}),
    ("reviewed", "failed"): frozenset({"user", "recorded_by_user"}),
    ("invited", "cancelled"): frozenset({"user"}),
    ("started", "cancelled"): frozenset({"user"}),
}
DECIDED_OUTCOMES = frozenset({"passed", "failed", "cancelled", "expired"})


def allowed(
    from_status: str | None,
    to_status: str,
    actor_kind: str,
    *,
    gating: bool = True,
    auto_pass_allowed: bool = False,
    score: Decimal | None = None,
    threshold: Decimal | None = None,
    provider: bool = False,
) -> bool:
    actors = TRANSITIONS.get((from_status, to_status))
    if actors is None:
        return False
    if provider and actor_kind == "system" and (from_status, to_status) == ("invited", "started"):
        return True
    if actor_kind not in actors:
        return False
    if (from_status, to_status) == ("reviewed", "passed") and actor_kind == "system":
        return (
            (not gating)
            and auto_pass_allowed
            and score is not None
            and threshold is not None
            and score >= threshold
        )
    return True


def require(from_status: str | None, to_status: str, actor_kind: str, **kw: object) -> None:
    if not allowed(from_status, to_status, actor_kind, **kw):  # type: ignore[arg-type]
        raise conflict(
            "transition_not_allowed",
            f"An invitation cannot move from {from_status or 'new'} to {to_status} ({actor_kind})",
            from_status=from_status,
            to_status=to_status,
            actor_kind=actor_kind,
        )


async def move(
    db: AsyncSession,
    inv: AssessmentInvitation,
    to_status: str,
    actor_kind: str,
    *,
    actor_id: object = None,
    result: str | None = None,
    **kw: object,
) -> None:
    """Validate and apply one transition; emits the domain-local status event and, for decided outcomes, the frozen
    ``AssessmentInvitationDecided``."""
    from_status = inv.status
    require(from_status, to_status, actor_kind, gating=inv.gating, **kw)
    inv.status = to_status
    if to_status in ("passed", "failed"):
        inv.result = result or ("pass" if to_status == "passed" else "fail")
        inv.decided_at = now()
    await db.flush()
    emit(
        db,
        AssessmentInvitationStatusChanged(
            tenant_id=inv.tenant_id,
            invitation_id=inv.id,
            assessment_id=inv.assessment_id,
            candidate_id=inv.candidate_id,
            application_id=inv.application_id,
            from_status=from_status,
            to_status=to_status,
            actor_kind=actor_kind,
            inviter_id=inv.invited_by_id if inv.invited_by_kind == "user" else None,
            actor_id=actor_id if hasattr(actor_id, "hex") else None,  # type: ignore[arg-type]
        ),
    )
    if to_status in DECIDED_OUTCOMES and inv.candidate_id is not None:
        emit(
            db,
            AssessmentInvitationDecided(
                tenant_id=inv.tenant_id,
                invitation_id=inv.id,
                assessment_id=inv.assessment_id,
                candidate_id=inv.candidate_id,
                application_id=inv.application_id,
                context_kind=inv.context_kind,
                context_id=inv.context_id,
                outcome=to_status,
                gating=inv.gating,
                actor_kind=actor_kind,
            ),
        )


__all__ = ["DECIDED_OUTCOMES", "HUMAN_DECIDERS", "TRANSITIONS", "allowed", "move", "require"]

"""Placement status machine (PRD §5.5, AD-4). ``transition_placement`` is the only writer of ``placements.status``:
it validates the move against ``TRANSITIONS``, writes ``placement_status_history`` and emits
``PlacementStatusChanged`` (plus ``PlacementActivated`` / ``PlacementDropped`` from the callers).

★ ``-> active`` (Joined) is human-only: ``ai``, ``workflow`` and ``system`` are refused (FR-65 / FR-106).
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import emit
from app.events.placements import PlacementStatusChanged
from app.models.placements import Placement, PlacementStatusHistory
from app.services.placements.common import now

_USER = frozenset({"user"})
TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("pre_joining", "joining_confirmed"): frozenset({"candidate", "secure_link", "user"}),
    ("pre_joining", "active"): _USER,
    ("joining_confirmed", "active"): _USER,
    ("pre_joining", "dropped"): _USER,
    ("joining_confirmed", "dropped"): _USER,
    # Declared for Epic 31 (extension / offboarding); triggered by the system only.
    ("active", "offboarding"): frozenset({"system"}),
    ("offboarding", "active"): frozenset({"system"}),
    ("offboarding", "ended"): frozenset({"system"}),
}
HUMAN_ONLY: frozenset[tuple[str, str]] = frozenset(
    {("pre_joining", "active"), ("joining_confirmed", "active")}
)


def not_allowed(from_status: str, to_status: str, actor_kind: str) -> ConflictError:
    return ConflictError(
        f"A placement cannot move from {from_status} to {to_status}",
        code="transition_not_allowed",
        details={"from": from_status, "to": to_status, "actor": actor_kind},
    )


def check_transition(from_status: str, to_status: str, actor_kind: str) -> None:
    allowed = TRANSITIONS.get((from_status, to_status))
    if allowed is None or actor_kind not in allowed:
        raise not_allowed(from_status, to_status, actor_kind)


async def transition_placement(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    placement: Placement,
    to: str,
    actor: ActorRef,
    reason: str | None = None,
    note: str | None = None,
) -> Placement:
    from_status = placement.status
    check_transition(from_status, to, actor.kind)
    placement.status = to
    placement.lock_version = (placement.lock_version or 1) + 1
    db.add(
        PlacementStatusHistory(
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            from_status=from_status,
            to_status=to,
            actor_kind=actor.kind,
            actor_id=actor.id,
            reason=reason,
            note=note,
            at=now(),
        )
    )
    await db.flush()
    emit(
        db,
        PlacementStatusChanged(
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            from_status=from_status,
            to_status=to,
            reason=reason,
        ),
    )
    return placement


async def record_creation(db: AsyncSession, placement: Placement, actor: ActorRef) -> None:
    """Write the ``— -> pre_joining`` history row (creation only)."""
    db.add(
        PlacementStatusHistory(
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            from_status=None,
            to_status=placement.status,
            actor_kind=actor.kind if actor.kind in ("user", "system") else "system",
            actor_id=actor.id,
            reason="created",
            at=now(),
        )
    )
    await db.flush()


def describe() -> list[dict[str, Any]]:
    return [{"from": a, "to": b, "actors": sorted(k)} for (a, b), k in TRANSITIONS.items()]

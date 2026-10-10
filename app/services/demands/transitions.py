"""Demand Status transition table (PRD §5.1, AD-4). Pure: no I/O.

Statuses: draft, pending_approval, open, on_hold, filled, cancelled, closed.
Actor kinds: user | candidate | system | workflow | ai | api_key. Only ``system`` may apply open -> filled;
everything else is human (``user``) only (FR-65).
"""

from typing import Literal

Status = Literal["draft", "pending_approval", "open", "on_hold", "filled", "cancelled", "closed"]
ActorKind = Literal["user", "candidate", "system", "workflow", "ai", "api_key", "agent", "app"]  # +agent (53), +app (66)

STATUSES: tuple[Status, ...] = (
    "draft",
    "pending_approval",
    "open",
    "on_hold",
    "filled",
    "cancelled",
    "closed",
)
TERMINAL: frozenset[Status] = frozenset({"cancelled", "closed"})

_USER: frozenset[ActorKind] = frozenset({"user"})
_SYSTEM: frozenset[ActorKind] = frozenset({"system"})

TRANSITIONS: dict[tuple[Status, Status], frozenset[ActorKind]] = {
    ("draft", "pending_approval"): _USER,  # submit, chain exists
    ("draft", "open"): _USER,  # submit, no chain (or all steps skipped)
    ("pending_approval", "open"): _USER,  # final step approved (approvals callback only)
    ("pending_approval", "draft"): _USER,  # reject / request changes (approvals callback only)
    ("open", "on_hold"): _USER,  # reason required
    ("on_hold", "open"): _USER,
    ("open", "filled"): _SYSTEM,  # accepted offers == positions
    ("filled", "open"): _USER,  # guard: positions > filled, or offer withdrawn after acceptance
    ("draft", "cancelled"): _USER,
    ("open", "cancelled"): _USER,
    ("on_hold", "cancelled"): _USER,
    ("filled", "closed"): _USER,
    ("open", "closed"): _USER,
    ("on_hold", "closed"): _USER,
}

# Target statuses whose transition needs a reason code.
REASON_REQUIRED: frozenset[Status] = frozenset({"on_hold", "cancelled", "closed"})

# Actor kinds that may never change demand status (FR-65).
HUMAN_ONLY_REJECTED: frozenset[str] = frozenset({"ai", "workflow", "agent", "app"})  # +agent (53), +app (66)

# Targets the generic ``POST /demands/{id}/transition`` endpoint refuses (system / submit only).
ENDPOINT_REFUSED_TARGETS: frozenset[Status] = frozenset({"filled", "pending_approval", "draft"})
# Sources from which the endpoint may not move to ``open`` (that is submit / approval).
ENDPOINT_REFUSED_OPEN_SOURCES: frozenset[Status] = frozenset({"draft", "pending_approval"})


def allowed_actors(from_status: str, to_status: str) -> frozenset[ActorKind] | None:
    return TRANSITIONS.get((from_status, to_status))  # type: ignore[arg-type]

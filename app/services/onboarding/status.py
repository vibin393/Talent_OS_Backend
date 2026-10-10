"""Onboarding status machines (Epic 23 A6, PRD §5.8, AD-4). ``ai`` is never an allowed actor.

★ ``-> waived`` is human-only (``onboarding:waive``, FR-181): ``workflow`` / ``system`` / ``ai`` get 409
``transition_not_allowed``. A Complete case is never reopened.
"""

from app.core.errors import ConflictError

_CANDIDATE = frozenset({"candidate"})
_USER = frozenset({"user"})
_SYSTEM = frozenset({"system"})
_USER_OR_SYSTEM = frozenset({"user", "system"})

TASK_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("pending", "submitted"): _CANDIDATE | _SYSTEM,  # system: connector outcomes (credential recorded)
    ("returned", "submitted"): _CANDIDATE | _SYSTEM,
    ("submitted", "approved"): _USER_OR_SYSTEM,
    ("submitted", "returned"): _USER | _SYSTEM,  # system: signature declined / credential rejected
    ("pending", "returned"): _SYSTEM,  # signature declined / expired before any submission
    ("pending", "waived"): _USER,
    ("submitted", "waived"): _USER,
    ("returned", "waived"): _USER,
    ("pending", "approved"): _SYSTEM,  # internal Task completed, reuse, auto-approve, signature completed
    ("returned", "approved"): _SYSTEM,  # re-issued signature completed
}
HUMAN_ONLY_TASK_TARGETS = frozenset({"waived"})

CASE_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("not_started", "in_progress"): _SYSTEM,
    ("in_progress", "complete"): _SYSTEM,
    ("not_started", "complete"): _SYSTEM,
    ("not_started", "cancelled"): _SYSTEM,
    ("in_progress", "cancelled"): _SYSTEM,
}


def not_allowed(kind: str, from_status: str, to_status: str, actor: str) -> ConflictError:
    return ConflictError(
        f"An onboarding {kind} cannot move from {from_status} to {to_status}",
        code="transition_not_allowed",
        details={"from": from_status, "to": to_status, "actor": actor},
    )


def check_task(from_status: str, to_status: str, actor: str) -> None:
    allowed = TASK_TRANSITIONS.get((from_status, to_status))
    if allowed is None or actor not in allowed or actor == "ai":
        raise not_allowed("task", from_status, to_status, actor)


def check_case(from_status: str, to_status: str, actor: str) -> None:
    allowed = CASE_TRANSITIONS.get((from_status, to_status))
    if allowed is None or actor not in allowed or actor == "ai":
        raise not_allowed("case", from_status, to_status, actor)

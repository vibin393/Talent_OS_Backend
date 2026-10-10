# ruff: noqa: E501
"""Outer state machine of an Immigration Case (Epic 46 A2, FR-233, AD-52). Declared table, no I/O.

``open -> in_stage`` (first stage move), ``in_stage -> in_stage`` (any stage key of the pinned version; a reason
is required when moving back), ``open | in_stage -> approved* | refused*`` (human only, ``immigration:manage``),
``open | in_stage -> withdrawn`` (reason). Terminal statuses are final: 409 ``transition_not_allowed``.
"""

from app.services.immigration.common import conflict

TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"in_stage", "approved", "refused", "withdrawn"}),
    "in_stage": frozenset({"in_stage", "approved", "refused", "withdrawn"}),
    "approved": frozenset(),
    "refused": frozenset(),
    "withdrawn": frozenset(),
}
HUMAN_ONLY = ("approved", "refused")
STATUS_LABELS = {
    "open": "Open",
    "in_stage": "In progress",
    "approved": "Approved",
    "refused": "Refused",
    "withdrawn": "Withdrawn",
}


def allowed(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, frozenset())


def check(from_status: str, to_status: str) -> None:
    if not allowed(from_status, to_status):
        raise conflict(
            "transition_not_allowed",
            f"A case that is {STATUS_LABELS.get(from_status, from_status).lower()} cannot move to {to_status}",
            from_status=from_status,
            to_status=to_status,
        )


def actions(status: str) -> list[str]:
    out: list[str] = []
    nxt = TRANSITIONS.get(status, frozenset())
    if "in_stage" in nxt:
        out.append("move_stage")
    if "approved" in nxt:
        out += ["approve", "refuse"]
    if "withdrawn" in nxt:
        out.append("withdraw")
    if status == "approved":
        out.append("record_permit")
    return out

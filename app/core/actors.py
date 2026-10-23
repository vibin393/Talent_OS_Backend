"""The one actor reference shared by every Phase 2 state change and approval (AD-4, AD-24).

Human kinds: ``user``, ``candidate``, ``client_user``, ``vendor_user``, ``secure_link`` (attributed to the link
addressee) and ``recorded_by_user`` (a User records a decision given offline, with evidence). ``system``,
``workflow``, ``ai``, ``agent`` (Epic 53, AD-60: an AI agent run, attributed to its agent principal and, when it
acts for a user, ``on_behalf_of``), ``api_key`` and ``app`` (Epic 66, AD-75: an App Principal bound to one Installation;
``id`` = the Installation, ``on_behalf_of`` = the user of a delegated token) are never human: they are refused for human-only (★) actions.
``AUTOMATED_KINDS`` is the one denylist; ``HUMAN_KINDS`` the allowlist.
"""

import dataclasses
import uuid
from typing import Any, Literal

from app.core.context import RequestContext
from app.core.errors import Forbidden

ActorKind = Literal[
    "user",
    "candidate",
    "client_user",
    "vendor_user",
    "secure_link",
    "recorded_by_user",
    "system",
    "workflow",
    "ai",
    "api_key",
    "agent",
    "app",
]
ACTOR_KINDS: tuple[str, ...] = (
    "user",
    "candidate",
    "client_user",
    "vendor_user",
    "secure_link",
    "recorded_by_user",
    "system",
    "workflow",
    "ai",
    "api_key",
    "agent",
    "app",
)
HUMAN_KINDS: frozenset[str] = frozenset(
    {"user", "candidate", "client_user", "vendor_user", "secure_link", "recorded_by_user"}
)
APPROVER_KINDS: frozenset[str] = frozenset({"user", "client_user", "secure_link", "recorded_by_user"})
# Epic 53 (AD-60): the single automated-kind denylist (never human, never an approver).
AUTOMATED_KINDS: frozenset[str] = frozenset({"system", "workflow", "ai", "agent", "api_key", "app"})
_AUTOMATED_CONTEXT_KINDS = AUTOMATED_KINDS


@dataclasses.dataclass(frozen=True, slots=True)
class ActorRef:
    kind: ActorKind
    # Principal id; for ``secure_link`` the link id; for ``recorded_by_user`` the recording User.
    id: uuid.UUID | None
    # ``secure_link`` -> the addressee ("client_user" | "contact", id); ``recorded_by_user`` -> the approver.
    attributed_to: tuple[str, uuid.UUID] | None = None
    on_behalf_of: uuid.UUID | None = None  # delegate decisions (FR-102)
    evidence_file_id: uuid.UUID | None = None  # required for ``recorded_by_user``

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS:
            msg = f"unknown actor kind {self.kind!r}"
            raise ValueError(msg)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": str(self.id) if self.id else None,
            "attributed_to": [self.attributed_to[0], str(self.attributed_to[1])]
            if self.attributed_to
            else None,
            "on_behalf_of": str(self.on_behalf_of) if self.on_behalf_of else None,
            "evidence_file_id": str(self.evidence_file_id) if self.evidence_file_id else None,
        }


def actor_from_ctx(ctx: RequestContext) -> ActorRef:
    """The actor of a request / task context. An automated context kind (``workflow`` rule runs, ``ai``,
    ``system`` jobs) wins over the principal that started it; otherwise the principal type decides."""
    if ctx.actor_kind == "agent":
        return ActorRef("agent", ctx.actor_id, on_behalf_of=ctx.on_behalf_of)
    if ctx.actor_kind in _AUTOMATED_CONTEXT_KINDS:
        return ActorRef(ctx.actor_kind, ctx.actor_id)  # type: ignore[arg-type]
    principal = ctx.principal
    kind = getattr(principal, "type", None)
    pid = getattr(principal, "id", None)
    if kind == "app":  # Epic 66 (AD-75): the Installation acts; a delegated token acts for its user
        return ActorRef("app", getattr(principal, "app_installation_id", None) or pid,
                        on_behalf_of=getattr(principal, "acting_user_id", None))  # fmt: skip
    if kind in {"user", "candidate", "client_user", "vendor_user", "api_key"}:
        return ActorRef(kind, pid)
    return ActorRef("system", None)


def is_human(actor: ActorRef) -> bool:
    return actor.kind in HUMAN_KINDS


def human_only_action() -> Forbidden:
    return Forbidden("Only a person can perform this action", code="human_only_action")


def require_human(actor: ActorRef) -> None:
    if not is_human(actor):
        raise human_only_action()


def acting_user_id(principal: object) -> uuid.UUID | None:
    """Epic 67: the user a principal acts as -- a user is itself, a delegated App Principal its ``acting_user_id``;
    app-only tokens, API keys and every other kind act as no user (``created_by`` / author stay NULL)."""
    kind = getattr(principal, "type", None)
    if kind == "user":
        return getattr(principal, "id", None)
    if kind == "app":
        return getattr(principal, "acting_user_id", None)
    return None

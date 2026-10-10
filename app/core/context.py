"""Per-request / per-task context held in a ContextVar (never in module globals)."""

import dataclasses
import uuid
from contextvars import ContextVar, Token
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    tenant_id: uuid.UUID | None = None
    principal: Any = None
    ip: str | None = None
    user_agent: str | None = None
    # Epic 10: automated actors. Workflow rule actions run with ``actor_kind="workflow"`` and
    # ``actor_id=<rule id>``; ``workflow_chain_depth`` counts rule-triggered-by-rule hops (loop protection).
    actor_kind: str | None = None
    actor_id: uuid.UUID | None = None
    workflow_chain_depth: int = 0
    # Epic 53 (AD-60): agent runs. ``agent_run_id`` / ``proposal_id`` are copied into ``audit_logs``
    # (``agent_run_id`` / ``via_proposal_id``); ``on_behalf_of`` is the user an agent run acts for.
    agent_run_id: uuid.UUID | None = None
    agent_key: str | None = None
    on_behalf_of: uuid.UUID | None = None
    proposal_id: uuid.UUID | None = None


_current: ContextVar[RequestContext | None] = ContextVar("talent_os_request_context", default=None)


def get_context() -> RequestContext:
    """Return the active context, or an empty one (no tenant, no principal) outside a request."""
    ctx = _current.get()
    return ctx if ctx is not None else RequestContext()


def set_context(ctx: RequestContext) -> Token[RequestContext | None]:
    return _current.set(ctx)


def reset_context(token: Token[RequestContext | None]) -> None:
    _current.reset(token)


def update_context(**changes: Any) -> RequestContext:
    """Replace the current context with a copy carrying ``changes`` (e.g. after authentication)."""
    ctx = dataclasses.replace(get_context(), **changes)
    _current.set(ctx)
    return ctx

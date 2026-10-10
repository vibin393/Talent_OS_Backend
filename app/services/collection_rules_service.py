# ruff: noqa: E501, D401
"""Facade of the ``collection_rules`` domain (Epic 63, FR-313..FR-315). Routers, tools, agents and tasks call these,
never the package internals."""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.collection_rules import CollectionPlanOut
from app.services.collection_rules import facts, policy_check
from app.services.collection_rules.facts import collections_owner
from app.services.collection_rules.holds import assert_not_held, hold, is_held, list_holds, release, state
from app.services.collection_rules.log import list_log
from app.services.collection_rules.rules import (
    create_rule,
    delete_rule,
    effective_rule,
    ensure_defaults,
    get_rule,
    list_rules,
    put_steps,
    update_rule,
)
from app.services.collection_rules.sends import (
    assert_sendable,
    check_message,
    send_message,
    send_statement,
    send_templated,
)
from app.services.collection_rules.ticks import (
    billing_kickoff,
    billing_owner,
    billing_prep_tick,
    daily_tick,
    previous_month,
    start_ptp_run,
)


async def plan_for(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    client_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> CollectionPlanOut:
    """"What the agent would do today" for one Client (no side effects beyond seeding the default rule)."""
    await ensure_defaults(db)
    return facts.to_out(await facts.load(db, client_id, now=now))


async def balance_version(db: AsyncSession, client_id: uuid.UUID) -> str:
    """Staleness version of collections sends: the Client's open / disputed balances and hold state."""
    import hashlib

    from app.services import receivables_service
    from app.services.collection_rules.holds import holds_map

    docs = await receivables_service.collectible_documents(db, client_id)
    client_hold, doc_holds = await holds_map(db, client_id)
    raw = "|".join(f"{d.id}:{d.open_amount}:{d.disputed_amount}:{d.status}" for d in sorted(docs, key=lambda d: str(d.id)))
    raw += f"#{client_hold}#" + ",".join(f"{k}:{v}" for k, v in sorted((str(k), v) for k, v in doc_holds.items()))
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


async def collections_owner_for(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """Accountable Owner of a Collections Agent run: ``client_collection_settings.owner_id`` -> the Client owner."""
    if subject is None or subject[0] != "client":
        return None
    return await collections_owner(db, subject[1])


async def billing_owner_for(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """Accountable Owner of a scheduled Billing Agent run: the creator of the Legal Entity's last invoice run (none ->
    no run; the runtime creates a Task for the Tenant Admin)."""
    if subject is None or subject[0] != "legal_entity":
        return None
    return await billing_owner(db, subject[1])


__all__ = [
    "assert_not_held",
    "assert_sendable",
    "balance_version",
    "billing_kickoff",
    "billing_owner_for",
    "billing_prep_tick",
    "check_message",
    "collections_owner_for",
    "create_rule",
    "daily_tick",
    "delete_rule",
    "effective_rule",
    "ensure_defaults",
    "get_rule",
    "hold",
    "is_held",
    "list_holds",
    "list_log",
    "list_rules",
    "plan_for",
    "policy_check",
    "previous_month",
    "put_steps",
    "release",
    "send_message",
    "send_statement",
    "send_templated",
    "start_ptp_run",
    "state",
    "update_rule",
]

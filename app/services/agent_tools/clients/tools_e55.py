# ruff: file-ignore[line-too-long]
"""Epic 55 Demand Agent read tools on clients (A3): ``clients.match_sender`` (the stored deterministic sender match;
never matched anew from arguments), ``contracts.for_client`` and ``rate_cards.lookup`` (names only -- no rates)."""

import uuid
from datetime import date
from typing import Any

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.schemas.demand_intake import MatchOut
from app.services.agent_tools.demands.common_e55 import OWNER_EPIC, In, Out, scoped

MAX_ROWS = 10


class MessageIn(In):
    message_id: uuid.UUID


async def _match_sender(tctx: Any, args: MessageIn) -> ToolOutcome:
    from app.services import demand_intake_service as intake

    out = await intake.match_of(scoped(tctx), tctx.db, args.message_id)
    refs: list[tuple[str, uuid.UUID]] = []
    if out.client_id:
        refs.append(("client", out.client_id))
    if out.contact_id:
        refs.append(("contact", out.contact_id))
    return ToolOutcome(
        out,
        refs=tuple(refs),
        target=("demand_intake_message", args.message_id),
        summary=f"match {out.status}",
    )


class ClientIn(In):
    client_id: uuid.UUID


class ContractRow(Out):
    id: uuid.UUID
    human_id: str
    title: str | None
    status: str
    start_date: date
    end_date: date | None


class ContractsOut(Out):
    contracts: list[ContractRow]


async def _contracts(tctx: Any, args: ClientIn) -> ToolOutcome:
    from app.services import clients_service
    from app.services.commercials.contracts import list_contracts

    ctx = scoped(tctx)
    await clients_service.get_client(ctx, tctx.db, args.client_id)  # 404 unless readable
    rows = [
        c
        for c in await list_contracts(ctx, tctx.db, client_id=args.client_id)
        if c.status in ("active", "pending_approval")
    ]
    out = ContractsOut(
        contracts=[
            ContractRow(
                id=c.id,
                human_id=c.human_id,
                title=c.title,
                status=c.status,
                start_date=c.start_date,
                end_date=c.end_date,
            )
            for c in rows[:MAX_ROWS]
        ]
    )
    return ToolOutcome(
        out,
        refs=tuple(("contract", c.id) for c in out.contracts),
        target=("client", args.client_id),
        summary=f"{len(out.contracts)} contract(s)",
    )


class RateCardRow(Out):
    id: uuid.UUID
    name: str
    contract_id: uuid.UUID | None
    status: str


class RateCardsOut(Out):
    rate_cards: list[RateCardRow]


async def _rate_cards(tctx: Any, args: ClientIn) -> ToolOutcome:
    from app.services import clients_service
    from app.services.commercials.rate_cards import list_client_cards

    ctx = scoped(tctx)
    await clients_service.get_client(ctx, tctx.db, args.client_id)
    rows = [
        r for r in await list_client_cards(ctx, tctx.db, args.client_id) if r.status in ("published", "draft")
    ]
    out = RateCardsOut(
        rate_cards=[
            RateCardRow(id=r.id, name=r.name, contract_id=r.contract_id, status=r.status)
            for r in rows[:MAX_ROWS]
        ]
    )
    return ToolOutcome(out, target=("client", args.client_id), summary=f"{len(out.rate_cards)} rate card(s)")


_CLIENT = ScopeRule(resource="client", target_arg="client_id")

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="clients.match_sender", version=1, owner_epic=OWNER_EPIC, title="Read the sender match",
        description="Read how the sender of this intake email was matched to a client and contact (exact contact address or an admin-verified domain; never by display name) and the accountable owner. It never matches anew and changes nothing.",
        input_model=MessageIn, output_model=MatchOut, permissions=("client:read", "contact:read"), risk="read",
        scope=ScopeRule(resource="demand_intake_message", target_arg="message_id"), handler=_match_sender,
    ),
    ToolSpec(
        key="contracts.for_client", version=1, owner_epic=OWNER_EPIC, title="List the client's contracts",
        description="List up to 10 active or pending contracts of a client (id, number, title, status, dates) to suggest the likely contract for a demand. It changes nothing.",
        input_model=ClientIn, output_model=ContractsOut, permissions=("contract:read",), risk="read", scope=_CLIENT,
        handler=_contracts, untrusted_output=frozenset({"contracts.title"}),
    ),
    ToolSpec(
        key="rate_cards.lookup", version=1, owner_epic=OWNER_EPIC, title="List the client's rate cards",
        description="List up to 10 rate cards of a client by name and status (no rates) to suggest the likely rate card for a demand. It changes nothing.",
        input_model=ClientIn, output_model=RateCardsOut, permissions=("rate_card:read",), risk="read", scope=_CLIENT,
        handler=_rate_cards, untrusted_output=frozenset({"rate_cards.name"}),
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()

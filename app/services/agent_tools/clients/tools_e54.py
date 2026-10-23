# ruff: file-ignore[line-too-long]
"""Epic 54 Copilot read tool ``clients.get`` (FR-277): a subset of the client 360 you can see
(``clients_service.client_360``; contacts only with ``contact:read``) plus the active placement count
(``placements.lists``, within your ``placement:read`` scope). It never changes a client."""

import uuid
from typing import Any

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import DomainError
from app.core.pagination import PageParams
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, principal_of, scoped, url_for


class ClientGetIn(In):
    client_id: uuid.UUID


class ContactItem(Out):
    id: uuid.UUID
    name: str
    title: str | None


class ClientGetOut(Out):
    id: uuid.UUID
    name: str
    status: str | None
    owner_id: uuid.UUID | None
    open_demands: int | None
    active_placements: int | None
    contacts: list[ContactItem]
    url: str | None


async def _get(tctx: Any, args: ClientGetIn) -> ToolOutcome:
    from app.services import clients_service
    from app.services.placements import lists

    ctx = scoped(tctx)
    out = await clients_service.client_360(ctx, tctx.db, args.client_id)
    active = None
    principal = principal_of(ctx)
    if principal is not None and principal.has("placement:read"):
        try:
            page = await lists.list_placements(
                ctx, tctx.db, PageParams(page=1, page_size=1), client_id=args.client_id, statuses=["active"]
            )
            active = page.total
        except DomainError:
            active = None
    contacts = [ContactItem(id=c.id, name=c.name, title=c.title) for c in (out.contacts or [])[:5]]
    data = ClientGetOut(
        id=out.client.id, name=out.client.name, status=out.client.status, owner_id=out.client.owner_id,
        open_demands=getattr(out.counts, "open_demands", None), active_placements=active, contacts=contacts,
        url=url_for("client", out.client.id),
    )  # fmt: skip
    return ToolOutcome(data, refs=(("client", data.id),), summary="client read")


register_tool(
    ToolSpec(
        key="clients.get",
        version=1,
        owner_epic=OWNER_EPIC,
        title="Read client",
        description=(
            "Read one client you can see: status, owner, open demand count, active placement count and up to five "
            "contacts (names and titles only, when you may read contacts). It returns no contact details and changes "
            "nothing. An unknown or inaccessible id returns not found."
        ),
        input_model=ClientGetIn,
        output_model=ClientGetOut,
        permissions=("client:read",),
        risk="read",
        scope=ScopeRule(resource="client", target_arg="client_id", cross_subject=True),
        handler=_get,
    )
)

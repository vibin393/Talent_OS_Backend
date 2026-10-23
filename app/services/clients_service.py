"""Clients and contacts (Epic 2, stories 2.1 / 2.2).

* Visibility follows the role scope of ``client:read`` / ``contact:read`` on the client's ``owner_id``
  (``scope_filter``); rows of another tenant are invisible through RLS. Invisible -> 404; visible but the
  caller's ``*:update`` / ``*:delete`` scope does not cover the row -> 403.
* Client names are unique per tenant, case-insensitively (409 ``client_exists``); on create, existing
  clients with a pg_trgm similarity >= 0.6 are returned as ``warnings``.
* Archived clients / contacts are hidden from lists unless ``include_archived``; archived clients do not
  accept new demands (``assert_client_accepts_demands`` -> 409 ``client_archived``, used by Epic 4).
* Every change is audited by the ``AuditedMixin`` listener; create/archive also add timeline entries.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, Text, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal, check, forbidden, scope_condition
from app.models.clients import Client, Contact
from app.models.identity import User
from app.schemas.clients import (
    Client360Counts,
    Client360Out,
    ClientCreate,
    ClientCreatedOut,
    ClientFilters,
    ClientOut,
    ClientUpdate,
    ContactCreate,
    ContactOut,
    ContactUpdate,
    Location,
    OwnerRef,
    QuickSearchHit,
    SimilarClient,
)
from app.services import activity_service

SIMILARITY_THRESHOLD = 0.6
MAX_WARNINGS = 5
CLIENT_NAME_CONSTRAINT = "uq_clients_tenant_id_name"
CONTACT_EMAIL_CONSTRAINT = "uq_contacts_client_id_email"


def _now() -> datetime:
    return datetime.now(UTC)


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def _constraint(exc: IntegrityError) -> str | None:
    todo: list[BaseException | None] = [exc.orig, exc.__cause__]
    seen: list[BaseException] = []
    while todo:
        cur = todo.pop()
        if cur is None or any(cur is x for x in seen):
            continue
        seen.append(cur)
        name = getattr(cur, "constraint_name", None)
        if isinstance(name, str):
            return name
        todo.extend([cur.__cause__, getattr(cur, "orig", None)])
    return None


def _client_exists() -> ConflictError:
    return ConflictError("A client with this name already exists", code="client_exists")


def _contact_exists() -> ConflictError:
    return ConflictError("A contact with this email already exists for this client", code="contact_exists")


def _invalid_reference(field: str, value: uuid.UUID) -> ValidationFailed:
    message = f"Unknown {field.replace('_', ' ')}: {value}"
    return ValidationFailed(
        message,
        code="invalid_reference",
        details={"fields": [{"field": field, "message": message, "type": "invalid_reference"}]},
    )


# ================================================================ scoping
def _client_cond(principal: Principal, permission: str) -> ColumnElement[bool] | None:
    return scope_condition(principal, permission, Client.owner_id)


def _scoped(stmt: Select[Any], principal: Principal, permission: str) -> Select[Any]:
    cond = _client_cond(principal, permission)
    return stmt if cond is None else stmt.where(cond)


async def _get_client(
    principal: Principal, db: AsyncSession, client_id: uuid.UUID, permission: str = "client:read"
) -> Client:
    """Load the client if ``client:read`` covers it (else 404) and ``permission`` does (else 403)."""
    check(principal, permission)
    client: Client | None = (
        await db.execute(_scoped(select(Client).where(Client.id == client_id), principal, "client:read"))
    ).scalar_one_or_none()
    if client is None:
        raise NotFound("Client not found")
    if permission != "client:read":
        cond = _client_cond(principal, permission)
        if cond is not None:
            allowed = (await db.execute(select(Client.id).where(Client.id == client.id, cond))).first()
            if allowed is None:
                raise forbidden(permission)
        locked: Client = (
            await db.execute(select(Client).where(Client.id == client.id).with_for_update())
        ).scalar_one()
        return locked
    return client


async def _get_contact(
    principal: Principal, db: AsyncSession, contact_id: uuid.UUID, permission: str = "contact:read"
) -> tuple[Contact, Client]:
    """Load the contact if ``contact:read`` covers its client's owner (else 404), ``permission`` else 403."""
    check(principal, permission)
    stmt = (
        select(Contact, Client).join(Client, Client.id == Contact.client_id).where(Contact.id == contact_id)
    )
    cond = scope_condition(principal, "contact:read", Client.owner_id)
    row = (await db.execute(stmt if cond is None else stmt.where(cond))).first()
    if row is None:
        raise NotFound("Contact not found")
    contact, client = row._tuple()
    if permission != "contact:read":
        wcond = scope_condition(principal, permission, Client.owner_id)
        if (
            wcond is not None
            and (await db.execute(select(Client.id).where(Client.id == client.id, wcond))).first() is None
        ):
            raise forbidden(permission)
        contact = (
            await db.execute(select(Contact).where(Contact.id == contact.id).with_for_update())
        ).scalar_one()
    return contact, client


# ---------------------------------------------------------------- activity hooks
async def client_access(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, write: bool) -> bool:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in ("user", "app", "api_key"):  # Epic 67: Public API
        return False
    permission = "client:update" if write else "client:read"
    if not principal.has(permission):
        return False
    stmt = _scoped(select(Client.id).where(Client.id == client_id), principal, permission)
    return (await db.execute(stmt)).first() is not None


async def contact_access(ctx: RequestContext, db: AsyncSession, contact_id: uuid.UUID, write: bool) -> bool:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in ("user", "app", "api_key"):  # Epic 67: Public API
        return False
    permission = "contact:update" if write else "contact:read"
    if not principal.has(permission):
        return False
    stmt = select(Contact.id).join(Client, Client.id == Contact.client_id).where(Contact.id == contact_id)
    cond = scope_condition(principal, permission, Client.owner_id)
    return (await db.execute(stmt if cond is None else stmt.where(cond))).first() is not None


activity_service.register_entity("client", client_access, plural="clients")
activity_service.register_entity("contact", contact_access, plural="contacts")


# ================================================================ output
async def _owners(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    return dict((await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).tuples().all())


async def _clients_out(db: AsyncSession, clients: Sequence[Client]) -> list[ClientOut]:
    names = await _owners(db, {c.owner_id for c in clients})
    return [
        ClientOut(
            id=c.id,
            name=c.name,
            industry=c.industry,
            website=c.website,
            locations=[Location.model_validate(loc) for loc in c.locations],
            gstin=c.gstin,
            owner=OwnerRef(id=c.owner_id, name=names[c.owner_id]) if c.owner_id in names else None,
            owner_id=c.owner_id,
            status=c.status,
            archived_at=c.archived_at,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in clients
    ]


async def _client_out(db: AsyncSession, client: Client) -> ClientOut:
    (out,) = await _clients_out(db, [client])
    return out


# ================================================================ clients
_SORTS: dict[str, tuple[ColumnElement[Any], ...]] = {
    "name": (Client.name.asc(),),
    "-name": (Client.name.desc(),),
    "created_at": (Client.created_at.asc(),),
    "-created_at": (Client.created_at.desc(),),
    "updated_at": (Client.updated_at.asc(),),
    "-updated_at": (Client.updated_at.desc(),),
    "status": (Client.status.asc(), Client.name.asc()),
    "-status": (Client.status.desc(), Client.name.asc()),
}


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_clients(
    ctx: RequestContext, db: AsyncSession, filters: ClientFilters, params: PageParams
) -> Page[ClientOut]:
    principal = _principal(ctx)
    stmt = _scoped(select(Client), principal, "client:read")
    if not filters.include_archived:
        stmt = stmt.where(Client.archived_at.is_(None))
    if filters.q and filters.q.strip():
        pattern = _like(filters.q.strip())
        stmt = stmt.where(
            or_(Client.name.ilike(pattern, escape="\\"), Client.industry.ilike(pattern, escape="\\"))
        )
    if filters.status is not None:
        stmt = stmt.where(Client.status == filters.status)
    if filters.owner_id is not None:
        stmt = stmt.where(Client.owner_id == filters.owner_id)
    stmt = stmt.order_by(*_SORTS[filters.sort], Client.id)
    page = await paginate(db, stmt, params)
    return Page[ClientOut](
        items=await _clients_out(db, page.items), total=page.total, page=page.page, page_size=page.page_size
    )


async def _check_owner(db: AsyncSession, owner_id: uuid.UUID) -> None:
    active = (await db.execute(select(User.is_active).where(User.id == owner_id))).scalar_one_or_none()
    if not active:  # unknown (or other tenant: RLS) or deactivated
        raise _invalid_reference("owner_id", owner_id)


async def similar_clients(
    db: AsyncSession, name: str, *, exclude_id: uuid.UUID | None = None
) -> list[SimilarClient]:
    """Clients whose name has pg_trgm similarity >= ``SIMILARITY_THRESHOLD`` (archived included)."""
    score = func.similarity(cast(Client.name, Text), name)
    stmt = select(Client.id, Client.name, score).where(score >= SIMILARITY_THRESHOLD)
    if exclude_id is not None:
        stmt = stmt.where(Client.id != exclude_id)
    rows = await db.execute(stmt.order_by(score.desc(), Client.name).limit(MAX_WARNINGS))
    return [SimilarClient(id=i, name=n, similarity=round(float(s), 3)) for i, n, s in rows.tuples()]


async def create_client(ctx: RequestContext, db: AsyncSession, data: ClientCreate) -> ClientCreatedOut:
    principal = _principal(ctx)
    check(principal, "client:create")
    owner_id = data.owner_id or principal.id
    if owner_id != principal.id:
        await _check_owner(db, owner_id)
    if (await db.execute(select(Client.id).where(Client.name == data.name))).first():
        raise _client_exists()
    warnings = await similar_clients(db, data.name)
    client = Client(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        name=data.name,
        industry=data.industry,
        website=data.website,
        locations=[loc.model_dump(exclude_none=True) for loc in data.locations or []],
        gstin=data.gstin,
        owner_id=owner_id,
        status=data.status,
    )
    db.add(client)
    try:
        await db.flush()
    except IntegrityError as exc:
        if _constraint(exc) != CLIENT_NAME_CONSTRAINT:
            raise
        raise _client_exists() from exc
    await db.refresh(client)
    await activity_service.add_system_entry(ctx, db, "client", client.id, "Client created")
    out = await _client_out(db, client)
    return ClientCreatedOut(**out.model_dump(), warnings=warnings)


async def get_client(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> ClientOut:
    return await _client_out(db, await _get_client(_principal(ctx), db, client_id))


async def update_client(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: ClientUpdate
) -> ClientOut:
    principal = _principal(ctx)
    client = await _get_client(principal, db, client_id, "client:update")
    sent = data.model_fields_set
    if "name" in sent and data.name is not None and data.name.lower() != client.name.lower():
        clash = await db.execute(select(Client.id).where(Client.name == data.name, Client.id != client.id))
        if clash.first():
            raise _client_exists()
        client.name = data.name
    elif "name" in sent and data.name is not None:
        client.name = data.name  # case-only change
    if "owner_id" in sent and data.owner_id is not None and data.owner_id != client.owner_id:
        await _check_owner(db, data.owner_id)
        client.owner_id = data.owner_id
    for field in ("industry", "website", "gstin"):
        if field in sent:
            setattr(client, field, getattr(data, field))
    if "locations" in sent:
        client.locations = [loc.model_dump(exclude_none=True) for loc in data.locations or []]
    if "status" in sent and data.status is not None:
        client.status = data.status
    try:
        await db.flush()
    except IntegrityError as exc:
        if _constraint(exc) != CLIENT_NAME_CONSTRAINT:
            raise
        raise _client_exists() from exc
    await db.refresh(client)
    return await _client_out(db, client)


async def archive_client(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> ClientOut:
    principal = _principal(ctx)
    client = await _get_client(principal, db, client_id, "client:delete")
    if client.archived_at is None:
        client.archived_at = _now()
        await db.flush()
        await activity_service.add_system_entry(ctx, db, "client", client.id, "Client archived")
    await db.refresh(client)
    return await _client_out(db, client)


async def unarchive_client(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> ClientOut:
    principal = _principal(ctx)
    client = await _get_client(principal, db, client_id, "client:delete")
    if client.archived_at is not None:
        client.archived_at = None
        await db.flush()
        await activity_service.add_system_entry(ctx, db, "client", client.id, "Client restored")
    await db.refresh(client)
    return await _client_out(db, client)


async def assert_client_accepts_demands(
    db: AsyncSession,
    client_id: uuid.UUID,
    ctx: RequestContext | None = None,  # ruff: ignore[unused-function-argument] - reserved (uniform service signature)
) -> None:
    """Raise unless the client can take a new demand (Epic 4).

    404 ``not_found`` if the client does not exist in the session's tenant (RLS), 409 ``client_archived``
    if it is archived. Scope checks on the demand side are the caller's job.
    """
    archived_at = (await db.execute(select(Client.archived_at).where(Client.id == client_id))).one_or_none()
    if archived_at is None:
        raise NotFound("Client not found")
    if archived_at[0] is not None:
        raise ConflictError("The client is archived and does not accept new demands", code="client_archived")


async def client_360(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> Client360Out:
    principal = _principal(ctx)
    client = await _get_client(principal, db, client_id)
    contacts: list[ContactOut] = []
    if principal.has("contact:read"):
        rows = await db.execute(
            select(Contact)
            .where(Contact.client_id == client.id, Contact.archived_at.is_(None))
            .order_by(Contact.name, Contact.id)
        )
        contacts = [ContactOut.model_validate(c) for c in rows.scalars()]
    # Epic 4: the caller's visible demands of this client (clients imports demands, never the reverse).
    from app.services import demands as demands_service

    demands = await demands_service.list_for_client(ctx, db, client.id)
    total_demands, open_demands = await demands_service.records.count_for_client(ctx, db, client.id)
    # Epic 6: the caller's visible submissions to this client (lazy import: clients never depends on pipeline).
    from app.services import pipeline as pipeline_service

    submission_summary = await pipeline_service.client_submission_summary(ctx, db, client.id)
    return Client360Out(
        client=await _client_out(db, client),
        contacts=contacts,
        counts=Client360Counts(
            contacts=len(contacts),
            activities=await activity_service.count_visible(ctx, db, "client", client.id),
            demands=total_demands,
            open_demands=open_demands,
            submissions=int(submission_summary["sent"]),
        ),
        demands=demands,
        submissions=list(submission_summary["recent"]),
    )


# ================================================================ contacts
async def list_contacts(
    ctx: RequestContext,
    db: AsyncSession,
    client_id: uuid.UUID,
    params: PageParams,
    *,
    include_archived: bool = False,
    q: str | None = None,
) -> Page[ContactOut]:
    principal = _principal(ctx)
    check(principal, "contact:read")
    client = await _get_client(principal, db, client_id)
    cond = scope_condition(principal, "contact:read", Client.owner_id)
    if (
        cond is not None
        and (await db.execute(select(Client.id).where(Client.id == client.id, cond))).first() is None
    ):
        raise NotFound("Client not found")
    stmt = select(Contact).where(Contact.client_id == client.id)
    if not include_archived:
        stmt = stmt.where(Contact.archived_at.is_(None))
    if q and q.strip():
        pattern = _like(q.strip())
        stmt = stmt.where(
            or_(Contact.name.ilike(pattern, escape="\\"), Contact.email.ilike(pattern, escape="\\"))
        )
    page = await paginate(db, stmt.order_by(Contact.name, Contact.id), params)
    return Page[ContactOut](
        items=[ContactOut.model_validate(c) for c in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def create_contact(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, data: ContactCreate
) -> ContactOut:
    principal = _principal(ctx)
    check(principal, "contact:create")
    client = await _get_client(principal, db, client_id)
    cond = scope_condition(principal, "contact:create", Client.owner_id)
    if (
        cond is not None
        and (await db.execute(select(Client.id).where(Client.id == client.id, cond))).first() is None
    ):
        raise forbidden("contact:create")
    if data.email is not None:
        dup = await db.execute(
            select(Contact.id).where(Contact.client_id == client.id, Contact.email == data.email)
        )
        if dup.first():
            raise _contact_exists()
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        client_id=client.id,
        name=data.name,
        title=data.title,
        email=data.email,
        phone=data.phone,
        type=data.type,
    )
    db.add(contact)
    try:
        await db.flush()
    except IntegrityError as exc:
        if _constraint(exc) != CONTACT_EMAIL_CONSTRAINT:
            raise
        raise _contact_exists() from exc
    await db.refresh(contact)
    await activity_service.add_system_entry(
        ctx, db, "client", client.id, "Contact added", {"contact_id": str(contact.id)}
    )
    return ContactOut.model_validate(contact)


async def get_contact(ctx: RequestContext, db: AsyncSession, contact_id: uuid.UUID) -> ContactOut:
    contact, _ = await _get_contact(_principal(ctx), db, contact_id)
    return ContactOut.model_validate(contact)


async def update_contact(
    ctx: RequestContext, db: AsyncSession, contact_id: uuid.UUID, data: ContactUpdate
) -> ContactOut:
    principal = _principal(ctx)
    contact, client = await _get_contact(principal, db, contact_id, "contact:update")
    sent = data.model_fields_set
    if "email" in sent and data.email is not None and data.email != (contact.email or "").lower():
        dup = await db.execute(
            select(Contact.id).where(
                Contact.client_id == client.id, Contact.email == data.email, Contact.id != contact.id
            )
        )
        if dup.first():
            raise _contact_exists()
    for field in ("title", "email", "phone"):
        if field in sent:
            setattr(contact, field, getattr(data, field))
    if "name" in sent and data.name is not None:
        contact.name = data.name
    if "type" in sent and data.type is not None:
        contact.type = data.type
    try:
        await db.flush()
    except IntegrityError as exc:
        if _constraint(exc) != CONTACT_EMAIL_CONSTRAINT:
            raise
        raise _contact_exists() from exc
    await db.refresh(contact)
    return ContactOut.model_validate(contact)


async def _set_contact_archived(
    ctx: RequestContext, db: AsyncSession, contact_id: uuid.UUID, archived: bool
) -> ContactOut:
    principal = _principal(ctx)
    contact, _ = await _get_contact(principal, db, contact_id, "contact:delete")
    if (contact.archived_at is not None) != archived:
        contact.archived_at = _now() if archived else None
        await db.flush()
    await db.refresh(contact)
    return ContactOut.model_validate(contact)


async def archive_contact(ctx: RequestContext, db: AsyncSession, contact_id: uuid.UUID) -> ContactOut:
    return await _set_contact_archived(ctx, db, contact_id, archived=True)


async def unarchive_contact(ctx: RequestContext, db: AsyncSession, contact_id: uuid.UUID) -> ContactOut:
    return await _set_contact_archived(ctx, db, contact_id, archived=False)


async def contact_belongs_to_client(db: AsyncSession, contact_id: uuid.UUID, client_id: uuid.UUID) -> bool:
    """For Epic 4 / 6: the contact exists (tenant session) and belongs to ``client_id``."""
    row = await db.execute(select(Contact.id).where(Contact.id == contact_id, Contact.client_id == client_id))
    return row.first() is not None


async def contacts_of_client(
    db: AsyncSession, client_id: uuid.UUID, contact_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """For Epic 6 (submission recipients): ``{id: {name, email, active}}`` of those contacts of the client."""
    if not contact_ids:
        return {}
    rows = await db.execute(
        select(Contact).where(Contact.client_id == client_id, Contact.id.in_(list(contact_ids)))
    )
    return {c.id: {"name": c.name, "email": c.email, "active": c.archived_at is None} for c in rows.scalars()}


async def client_name(db: AsyncSession, client_id: uuid.UUID) -> str | None:
    """For Epic 6 (consent notices, submission emails): the client's display name (tenant session)."""
    return (await db.execute(select(Client.name).where(Client.id == client_id))).scalar_one_or_none()


# ================================================================ quick search (Epic 5 global search)
async def quick_search(ctx: RequestContext, db: AsyncSession, q: str, limit: int = 5) -> list[QuickSearchHit]:
    """Prefix/substring match on client names and contact names/emails the caller can see."""
    principal = _principal(ctx)
    term = q.strip()
    if len(term) < 2:
        return []
    pattern = _like(term)
    hits: list[QuickSearchHit] = []
    if principal.has("client:read"):
        stmt = _scoped(select(Client), principal, "client:read").where(
            Client.archived_at.is_(None), Client.name.ilike(pattern, escape="\\")
        )
        for c in (await db.execute(stmt.order_by(Client.name).limit(limit))).scalars():
            hits.append(QuickSearchHit(id=c.id, kind="client", title=c.name, subtitle=c.industry))
    if principal.has("contact:read"):
        stmt2 = (
            select(Contact, Client.name)
            .join(Client, Client.id == Contact.client_id)
            .where(
                Contact.archived_at.is_(None),
                or_(Contact.name.ilike(pattern, escape="\\"), Contact.email.ilike(pattern, escape="\\")),
            )
        )
        cond = scope_condition(principal, "contact:read", Client.owner_id)
        if cond is not None:
            stmt2 = stmt2.where(cond)
        for contact, client_name in (await db.execute(stmt2.order_by(Contact.name).limit(limit))).tuples():
            hits.append(
                QuickSearchHit(
                    id=contact.id,
                    kind="contact",
                    title=contact.name,
                    subtitle=client_name,
                    client_id=contact.client_id,
                )
            )
    return hits

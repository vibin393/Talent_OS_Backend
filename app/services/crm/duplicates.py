"""Lead duplicate detection (FR-83, A2): against Clients and open Leads by name, website domain and GSTIN."""

import uuid
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.models.clients import Client
from app.models.crm import Lead
from app.schemas.crm import DuplicateMatch
from app.services import clients_service

MAX_MATCHES = 10


def website_domain(website: str | None) -> str | None:
    """Lower-cased host without ``www.`` (``https://www.acme.com/x`` -> ``acme.com``)."""
    if not website:
        return None
    raw = website.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    host = (urlsplit(raw).hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _client_domain_expr() -> object:
    host = func.lower(func.regexp_replace(func.coalesce(Client.website, ""), r"^[a-zA-Z]+://", ""))
    host = func.split_part(func.split_part(func.split_part(host, "/", 1), ":", 1), "?", 1)
    return func.regexp_replace(host, r"^www\.", "")


async def find_duplicates(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    company_name: str | None,
    website: str | None,
    gstin: str | None,
    exclude_lead_id: uuid.UUID | None = None,
    *,
    strong_only: bool = False,
) -> list[DuplicateMatch]:
    """Return matches by exact name / domain / GSTIN, plus similar Client names (pg_trgm) unless ``strong_only``."""
    name = (company_name or "").strip()
    domain = website_domain(website)
    gst = (gstin or "").strip().upper() or None
    found: dict[tuple[str, uuid.UUID], DuplicateMatch] = {}

    def add(kind: str, id_: uuid.UUID, label: str, reason: str, human_id: str | None = None) -> None:
        found.setdefault(
            (kind, id_), DuplicateMatch(kind=kind, id=id_, name=label, reason=reason, human_id=human_id)
        )  # type: ignore[arg-type]

    # ---- clients: exact (citext) name, domain, GSTIN, then similar names
    conds = []
    if name:
        conds.append(Client.name == name)
    if domain:
        conds.append(_client_domain_expr() == domain)
    if gst:
        conds.append(Client.gstin == gst)
    if conds:
        rows = await db.execute(
            select(Client.id, Client.name, Client.website, Client.gstin).where(or_(*conds)).limit(MAX_MATCHES)
        )
        for cid, cname, cweb, cgst in rows.tuples():
            if name and cname.lower() == name.lower():
                add("client", cid, cname, "name")
            elif gst and cgst == gst:
                add("client", cid, cname, "gstin")
            elif domain and website_domain(cweb) == domain:
                add("client", cid, cname, "domain")
    if name and not strong_only:
        for similar in await clients_service.similar_clients(db, name):
            add("client", similar.id, similar.name, "similar_name")

    # ---- open leads (not archived, not converted)
    lconds = []
    if name:
        lconds.append(Lead.company_name == name)
    if domain:
        lconds.append(Lead.website_domain == domain)
    if gst:
        lconds.append(Lead.gstin == gst)
    if lconds:
        stmt = select(Lead.id, Lead.company_name, Lead.website_domain, Lead.gstin, Lead.human_id).where(
            or_(*lconds), Lead.archived_at.is_(None), Lead.status != "converted"
        )
        if exclude_lead_id is not None:
            stmt = stmt.where(Lead.id != exclude_lead_id)
        for lid, lname, ldomain, lgst, hid in (await db.execute(stmt.limit(MAX_MATCHES))).tuples():
            if name and lname.lower() == name.lower():
                add("lead", lid, lname, "name", hid)
            elif gst and lgst == gst:
                add("lead", lid, lname, "gstin", hid)
            elif domain and ldomain == domain:
                add("lead", lid, lname, "domain", hid)
    return list(found.values())[: MAX_MATCHES * 2]


def possible_duplicate(matches: list[DuplicateMatch]) -> ConflictError:
    return ConflictError(
        "Possible duplicate of an existing client or lead",
        code="possible_duplicate",
        details={"matches": [m.model_dump(mode="json") for m in matches]},
    )

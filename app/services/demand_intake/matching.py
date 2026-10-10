# ruff: noqa: E501, D401, D400, D415
"""Sender matching (Epic 55, spec 0.4, FR-281). **Never by display name.**

1. **Contact address** -- exact, case-insensitive ``From`` = an active Contact's email -> that Contact and Client
   (more than one Client -> ``ambiguous``).
2. **Verified domain** -- the ``From`` domain is mapped in ``demand_intake_sender_domains`` -> that Client, contact
   unknown (a domain mapped to several Clients -> ``ambiguous``).
3. Otherwise ``unmatched``.

Sender authentication (``require_sender_auth``): when ``Authentication-Results`` is present and neither ``dkim=pass``
nor ``spf=pass`` aligns with the ``From`` domain -> ``unmatched`` / ``sender_not_authenticated``; absent -> allowed,
``auth_result=unknown``.

Forwarded mail (``accept_forwarded``): a message sent by an active internal User is matched on the original ``From``
parsed from the forwarded header block -- **step 1 only** -- and the forwarding User is the Accountable Owner.
"""

import dataclasses
import email.utils
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clients import Client, Contact
from app.models.demand_intake import DemandAgentSettings, DemandIntakeSenderDomain

_FROM_LINE = re.compile(r"^\s*>?\s*From:\s*(?P<value>.+)$", re.IGNORECASE | re.MULTILINE)
_AUTH_ITEM = re.compile(r"\b(?P<method>dkim|spf)\s*=\s*(?P<result>[a-z]+)(?P<rest>[^;]*)", re.IGNORECASE)
_PROP = re.compile(
    r"\b(?:header\.d|header\.i|smtp\.mailfrom|smtp\.helo)\s*=\s*(?P<value>[^\s;]+)", re.IGNORECASE
)


@dataclasses.dataclass(frozen=True, slots=True)
class MatchResult:
    status: str  # matched | unmatched | ambiguous
    method: str | None = None
    client_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    reason: str | None = None
    auth_result: str | None = None
    forwarded_by_user_id: uuid.UUID | None = None
    original_from: str | None = None


def parse_address(value: str | None) -> tuple[str | None, str | None]:
    """``(name, address)`` of a ``From`` header value (address lower-cased); ``(None, None)`` when absent."""
    if not value:
        return None, None
    name, addr = email.utils.parseaddr(value)
    addr = addr.strip().lower()
    if "@" not in addr:
        return (name or None), None
    return (name.strip() or None), addr


def domain_of(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().lower().rstrip(".") or None


def _aligned(candidate: str, from_domain: str) -> bool:
    c = candidate.lower().strip().strip("<>").rstrip(".")
    if "@" in c:
        c = c.rsplit("@", 1)[1]
    return c == from_domain or c.endswith("." + from_domain) or from_domain.endswith("." + c)


def auth_result(header: str | None, from_domain: str | None) -> str:
    """``pass`` (dkim or spf passes, aligned with the From domain), ``fail`` or ``unknown`` (no header)."""
    if not header:
        return "unknown"
    if not from_domain:
        return "fail"
    for item in _AUTH_ITEM.finditer(header):
        if item.group("result").lower() != "pass":
            continue
        props = [m.group("value") for m in _PROP.finditer(item.group("rest"))]
        if any(_aligned(p, from_domain) for p in props):
            return "pass"
    return "fail"


def forwarded_original_from(body: str) -> str | None:
    """The original sender address from a forwarded header block (first ``From:`` line after a forward marker)."""
    marker = re.search(
        r"(-{2,}\s*Forwarded message\s*-{2,}|-{2,}\s*Original Message\s*-{2,}|Begin forwarded message:)",
        body or "",
        re.IGNORECASE,
    )
    region = body[marker.end() :] if marker else body or ""
    line = _FROM_LINE.search(region)
    if not line:
        return None
    _, addr = parse_address(line.group("value").strip())
    return addr


async def internal_user_id(db: AsyncSession, address: str | None) -> uuid.UUID | None:
    if not address:
        return None
    from app.models.identity import User

    found = (
        await db.execute(select(User.id).where(User.email == address.lower(), User.is_active.is_(True)))
    ).first()
    return found[0] if found else None


async def _contact_matches(db: AsyncSession, address: str) -> list[tuple[uuid.UUID, uuid.UUID]]:
    rows = await db.execute(
        select(Contact.id, Contact.client_id)
        .join(Client, Client.id == Contact.client_id)
        .where(Contact.email == address.lower(), Contact.archived_at.is_(None), Client.archived_at.is_(None))
        .order_by(Contact.created_at)
    )
    return [(r[0], r[1]) for r in rows]


async def _domain_matches(db: AsyncSession, domain: str) -> list[uuid.UUID]:
    rows = await db.execute(
        select(DemandIntakeSenderDomain.client_id)
        .join(Client, Client.id == DemandIntakeSenderDomain.client_id)
        .where(DemandIntakeSenderDomain.domain == domain.lower(), Client.archived_at.is_(None))
    )
    return sorted({r[0] for r in rows}, key=str)


async def client_owner(db: AsyncSession, client_id: uuid.UUID) -> uuid.UUID | None:
    from app.services import entity_refs

    owner = (await db.execute(select(Client.owner_id).where(Client.id == client_id))).scalar_one_or_none()
    if owner is not None and owner in await entity_refs.active_user_ids(db, [owner]):
        return owner
    return None


async def _owner_for(
    db: AsyncSession, client_id: uuid.UUID, settings: DemandAgentSettings
) -> uuid.UUID | None:
    return await client_owner(db, client_id) or settings.fallback_owner_id


async def _by_contact(
    db: AsyncSession, address: str, settings: DemandAgentSettings, method: str
) -> MatchResult | None:
    hits = await _contact_matches(db, address)
    clients = {c for _, c in hits}
    if len(clients) > 1:
        return MatchResult("ambiguous", reason="contact_on_several_clients")
    if len(clients) == 1:
        contact_id, client_id = hits[0]
        return MatchResult(
            "matched", method=method, client_id=client_id, contact_id=contact_id,
            owner_user_id=await _owner_for(db, client_id, settings),
        )  # fmt: skip
    return None


async def match_sender(
    db: AsyncSession,
    settings: DemandAgentSettings,
    *,
    from_address: str | None,
    auth_header: str | None,
    body_text: str,
) -> MatchResult:
    """Deterministic match of one received email (display names are never used)."""
    from_domain = domain_of(from_address)
    auth = auth_result(auth_header, from_domain)
    if from_address is None or from_domain is None:
        return MatchResult("unmatched", reason="no_sender_address", auth_result=auth)
    if settings.require_sender_auth and auth == "fail":
        return MatchResult("unmatched", reason="sender_not_authenticated", auth_result=auth)
    forwarder = await internal_user_id(db, from_address) if settings.accept_forwarded else None
    if forwarder is not None:
        original = forwarded_original_from(body_text)
        if original is None or original == from_address:
            return MatchResult(
                "unmatched",
                reason="forwarded_original_unknown",
                auth_result=auth,
                forwarded_by_user_id=forwarder,
            )
        found = await _by_contact(db, original, settings, "forwarded_contact")
        if found is None:
            return MatchResult(
                "unmatched", reason="forwarded_sender_not_a_contact", auth_result=auth,
                forwarded_by_user_id=forwarder, original_from=original,
            )  # fmt: skip
        if found.status != "matched":
            return dataclasses.replace(
                found, auth_result=auth, forwarded_by_user_id=forwarder, original_from=original
            )
        return dataclasses.replace(
            found,
            owner_user_id=forwarder,
            auth_result=auth,
            forwarded_by_user_id=forwarder,
            original_from=original,
        )
    found = await _by_contact(db, from_address, settings, "contact_email")
    if found is not None:
        return dataclasses.replace(found, auth_result=auth)
    domain_clients = await _domain_matches(db, from_domain)
    if len(domain_clients) > 1:
        return MatchResult("ambiguous", reason="domain_on_several_clients", auth_result=auth)
    if len(domain_clients) == 1:
        client_id = domain_clients[0]
        return MatchResult(
            "matched", method="verified_domain", client_id=client_id,
            owner_user_id=await _owner_for(db, client_id, settings), auth_result=auth,
        )  # fmt: skip
    return MatchResult("unmatched", reason="sender_unknown", auth_result=auth)


def explanation(method: str | None, from_address: str | None, original_from: str | None = None) -> str | None:
    if method == "contact_email":
        return f"Matched by contact email: {from_address}"
    if method == "verified_domain":
        return f"Matched by verified domain: {domain_of(from_address)}"
    if method == "forwarded_contact":
        return f"Forwarded by a colleague; matched by contact email: {original_from or 'the original sender'}"
    if method == "manual":
        return "Client chosen by a person"
    return None


__all__ = [
    "MatchResult",
    "auth_result",
    "client_owner",
    "domain_of",
    "explanation",
    "forwarded_original_from",
    "internal_user_id",
    "match_sender",
    "parse_address",
]

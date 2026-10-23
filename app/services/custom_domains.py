# ruff: file-ignore[line-too-long]
"""Custom Career Portal domains (Phase 2 Epic 33, FR-174, AD-38). Re-exported by ``platform_service``.

* ``custom_domains`` is a platform table (no RLS, no PII), read and written through ``platform_session`` with an
  explicit ``tenant_id`` filter. One live (non-removed) domain per tenant; a host can be live for one tenant only
  (409 ``domain_taken``). The platform's own hosts are refused.
* Verification: DNS TXT ``_talentos-verify.<host>`` = ``talentos-verify=<txt_token>`` (``dnspython``), plus a
  CNAME to ``CAREERS_CNAME_TARGET`` for serving. TLS certificates are managed by hosting (deferred, AD-38).
* ``tenant_for_host`` is cached for 5 minutes (Redis via the connections key-value store, in-process fallback)
  and invalidated on every change, so a removed host stops resolving within the cache TTL.
* ``check_domains`` (beat ``platform.check_custom_domains``, queue ``maintenance``, every 10 minutes): pending /
  failed domains are checked each run, verified ones daily; a verified domain whose TXT is missing on 3
  consecutive daily checks becomes ``failed`` and the tenant admins are emailed.
"""

import ipaddress
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.settings import get_settings
from app.events.platform import CustomDomainFailed, CustomDomainRemoved, CustomDomainVerified
from app.models.identity import Tenant
from app.models.platform import CustomDomain
from app.schemas.messaging import CustomDomainOut

TXT_PREFIX = "_talentos-verify"
TXT_VALUE_PREFIX = "talentos-verify="
CACHE_TTL_SECONDS = 300
VERIFIED_RECHECK = timedelta(days=1)
FAILURES_BEFORE_FAILED = 3
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

# ``(name) -> TXT strings``; tests replace it (``set_resolver``). The default uses dnspython.
TxtResolver = Callable[[str], Awaitable[list[str]]]


async def _dns_txt(name: str) -> list[str]:
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    try:
        answer = await dns.asyncresolver.resolve(name, "TXT", lifetime=5.0)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        return []
    out: list[str] = []
    for record in answer:
        out.append(b"".join(record.strings).decode("utf-8", errors="replace"))
    return out


_resolver: TxtResolver = _dns_txt


def set_resolver(fn: TxtResolver | None) -> None:
    global _resolver  # tests
    _resolver = fn or _dns_txt


def _now() -> datetime:
    return datetime.now(UTC)


# ================================================================ cache
_memory: dict[str, tuple[str, datetime]] = {}


def _key(host: str) -> str:
    return f"custom_domain:{host}"


async def _cache_get(host: str) -> str | None:
    hit = _memory.get(host)
    if hit is not None and hit[1] > _now():
        return hit[0]
    try:
        from app.services.connections.store import get_store

        return await get_store().get(_key(host))
    except Exception:  # cache is best effort
        return None


async def _cache_put(host: str, value: str) -> None:
    _memory[host] = (value, _now() + timedelta(seconds=CACHE_TTL_SECONDS))
    try:
        from app.services.connections.store import get_store

        await get_store().put(_key(host), value, CACHE_TTL_SECONDS)
    except Exception:
        logger.debug("custom domain cache put skipped")


async def invalidate(host: str) -> None:
    _memory.pop(host, None)
    try:
        from app.services.connections.store import get_store

        await get_store().pop(_key(host))
    except Exception:
        logger.debug("custom domain cache invalidation skipped")


def clear_cache() -> None:
    _memory.clear()


# ================================================================ helpers
def normalise_host(raw: str | None) -> str:
    host = (raw or "").strip().lower().rstrip(".")
    if "://" in host:
        host = urlparse(host).hostname or ""
    return host.split(":", 1)[0]


def _platform_hosts() -> set[str]:
    s = get_settings()
    hosts = {"localhost"}
    for url in (s.frontend_url, s.oauth_redirect_base):
        h = urlparse(url).hostname
        if h:
            hosts.add(h.lower())
    if s.careers_root_domain:
        hosts.add(s.careers_root_domain.strip(".").lower())
    return hosts


def validate_host(raw: str) -> str:
    host = normalise_host(raw)
    labels = host.split(".")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if (
        is_ip
        or len(host) > 253
        or len(labels) < 2
        or not all(_LABEL.match(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise ValidationFailed(
            "Enter a valid host name, e.g. careers.example.in",
            code="invalid_host",
            details={"fields": {"host": "invalid"}},
        )
    for platform in _platform_hosts():
        if host == platform or host.endswith(f".{platform}"):
            raise ValidationFailed(
                "This host belongs to the platform",
                code="platform_domain",
                details={"fields": {"host": "platform"}},
            )
    return host


def txt_name(host: str) -> str:
    return f"{TXT_PREFIX}.{host}"


def _out(row: CustomDomain) -> CustomDomainOut:
    return CustomDomainOut(
        id=row.id,
        host=str(row.host),
        status=row.status,  # type: ignore[arg-type]
        txt_name=txt_name(str(row.host)),
        txt_value=f"{TXT_VALUE_PREFIX}{row.txt_token}",
        cname_target=get_settings().careers_cname_target or None,
        verified_at=row.verified_at,
        last_checked_at=row.last_checked_at,
    )


def _tenant(ctx: RequestContext) -> uuid.UUID:
    if ctx.tenant_id is None:
        raise NotFound("Resource not found")
    return ctx.tenant_id


async def _live(pdb: object, tenant_id: uuid.UUID) -> CustomDomain | None:
    return (
        await pdb.execute(  # type: ignore[attr-defined]
            select(CustomDomain).where(CustomDomain.tenant_id == tenant_id, CustomDomain.status != "removed")
        )
    ).scalar_one_or_none()


# ================================================================ API
async def get_custom_domain(ctx: RequestContext) -> CustomDomainOut | None:
    async with platform_session() as pdb:
        row = await _live(pdb, _tenant(ctx))
        return _out(row) if row is not None else None


async def add_custom_domain(ctx: RequestContext, db: AsyncSession, host: str) -> CustomDomainOut:
    tenant_id = _tenant(ctx)
    host = validate_host(host)
    async with platform_session() as pdb:
        if await _live(pdb, tenant_id) is not None:
            raise ConflictError("Remove the current custom domain first", code="domain_exists")
        taken = (
            await pdb.execute(
                select(CustomDomain.id).where(CustomDomain.host == host, CustomDomain.status != "removed")
            )
        ).first()
        if taken is not None:
            raise ConflictError("This domain is already used by another workspace", code="domain_taken")
        row = CustomDomain(
            id=uuid.uuid4(),
            host=host,
            tenant_id=tenant_id,
            txt_token=secrets.token_urlsafe(24),
            status="pending",
        )
        pdb.add(row)
        try:
            await pdb.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "This domain is already used by another workspace", code="domain_taken"
            ) from exc
        await pdb.refresh(row)
        out = _out(row)
    audit.record("custom_domain.added", "custom_domain", out.id, session=db, host=host)
    await invalidate(host)
    return out


async def _check(row: CustomDomain) -> bool:
    try:
        records = await _resolver(txt_name(str(row.host)))
    except Exception as exc:
        logger.warning("Custom domain TXT lookup failed (domain={}, error={})", row.id, type(exc).__name__)
        return False
    expected = f"{TXT_VALUE_PREFIX}{row.txt_token}"
    return any(r.strip().strip('"') == expected for r in records)


async def _apply_check(pdb: object, row: CustomDomain, now: datetime) -> str:
    """Check one domain and update it; returns its status. Emits events / emails on transitions."""
    ok = await _check(row)
    row.last_checked_at = now
    previous = row.status
    if ok:
        row.consecutive_failures = 0
        if row.status != "verified":
            row.status, row.verified_at = "verified", now
    elif row.status == "verified":
        row.consecutive_failures += 1
        if row.consecutive_failures >= FAILURES_BEFORE_FAILED:
            row.status = "failed"
    elif row.status == "pending":
        row.consecutive_failures += 1
    await pdb.flush()  # type: ignore[attr-defined]
    if row.status != previous:
        await invalidate(str(row.host))
        if row.status == "verified":
            emit(pdb, CustomDomainVerified(tenant_id=row.tenant_id, domain_id=row.id))  # type: ignore[arg-type]
        elif row.status == "failed":
            emit(pdb, CustomDomainFailed(tenant_id=row.tenant_id, domain_id=row.id))  # type: ignore[arg-type]
            await _notify_failed(row)
    return row.status


async def _notify_failed(row: CustomDomain) -> None:
    from app.core.database import tenant_session
    from app.services import identity_bridge
    from app.services.privacy.common import email_users

    try:
        async with tenant_session(row.tenant_id) as db:
            admins = await identity_bridge.tenant_admins(db)
        await email_users(
            admins,
            "Career site custom domain stopped verifying",
            f"The DNS TXT record for {row.host} was missing on {FAILURES_BEFORE_FAILED} daily checks. The career site "
            "is served on its default address until the record is restored and the domain is verified again.\n",
            tenant_id=row.tenant_id,
            kind="custom_domain_failed",
        )
    except Exception:
        logger.exception("Could not notify admins about custom domain {}", row.id)


async def verify_custom_domain(ctx: RequestContext) -> CustomDomainOut:
    tenant_id = _tenant(ctx)
    async with platform_session() as pdb:
        row = await _live(pdb, tenant_id)
        if row is None:
            raise NotFound("No custom domain configured")
        if row.status == "failed":
            row.status, row.consecutive_failures = "pending", 0
        await _apply_check(pdb, row, _now())
        await pdb.refresh(row)
        return _out(row)


async def remove_custom_domain(ctx: RequestContext, db: AsyncSession) -> None:
    tenant_id = _tenant(ctx)
    async with platform_session() as pdb:
        row = await _live(pdb, tenant_id)
        if row is None:
            raise NotFound("No custom domain configured")
        row.status, row.removed_at = "removed", _now()
        await pdb.flush()
        host, domain_id = str(row.host), row.id
        emit(pdb, CustomDomainRemoved(tenant_id=tenant_id, domain_id=domain_id))
    audit.record("custom_domain.removed", "custom_domain", domain_id, session=db, host=host)
    await invalidate(host)


async def tenant_for_host(host: str | None) -> uuid.UUID | None:
    """The tenant whose **verified** custom domain is ``host`` (cached 5 minutes)."""
    host = normalise_host(host)
    if not host or host in _platform_hosts():
        return None
    cached = await _cache_get(host)
    if cached is not None:
        return uuid.UUID(cached) if cached != "-" else None
    async with platform_session() as pdb:
        tenant_id = (
            await pdb.execute(
                select(CustomDomain.tenant_id).where(
                    CustomDomain.host == host, CustomDomain.status == "verified"
                )
            )
        ).scalar_one_or_none()
    await _cache_put(host, str(tenant_id) if tenant_id else "-")
    return tenant_id


async def slug_for_host(host: str | None) -> str | None:
    tenant_id = await tenant_for_host(host)
    if tenant_id is None:
        return None
    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tenant_id)
    return str(tenant.slug) if tenant is not None and tenant.status == "active" else None


async def verified_host_for_tenant(tenant_id: uuid.UUID) -> str | None:
    async with platform_session() as pdb:
        return (
            await pdb.execute(
                select(CustomDomain.host).where(
                    CustomDomain.tenant_id == tenant_id, CustomDomain.status == "verified"
                )
            )
        ).scalar_one_or_none()


async def check_domains(now: datetime | None = None) -> dict[str, int]:
    """Beat body: pending / failed every run, verified once a day."""
    now = now or _now()
    counts = {"checked": 0, "verified": 0, "failed": 0}
    async with platform_session() as pdb:
        rows = list(
            (
                await pdb.execute(
                    select(CustomDomain)
                    .where(CustomDomain.status.in_(("pending", "verified", "failed")))
                    .limit(5000)
                )
            ).scalars()
        )
        for row in rows:
            if (
                row.status == "verified"
                and row.last_checked_at
                and now - row.last_checked_at < VERIFIED_RECHECK
            ):
                continue
            status = await _apply_check(pdb, row, now)
            counts["checked"] += 1
            counts[status] = counts.get(status, 0) + 1
    return counts

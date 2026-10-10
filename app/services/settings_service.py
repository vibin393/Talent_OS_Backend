"""Tenant settings hub (Epic 12, story 12.8, FR-74).

* ``tenant_settings`` (one row per tenant, created lazily) holds four jsonb sections owned here: ``company``,
  ``regional``, ``id_prefixes`` and ``link_expiries``. Settings owned by other domains stay there; the hub
  catalog (``catalog``) links to them and marks unbuilt ones ``available: false``.
* ``link_ttl(db, purpose)`` is the only source of secure-link expiries; ``id_prefix(db, key)`` the only
  source of human-ID prefixes (the sequence key never changes, so numbering continues).
* Every PATCH is audited by the ``AuditedMixin`` listener and additionally records
  ``settings.updated {section, changed_keys}``. Errors are 422 ``validation_failed`` with
  ``details.fields{path: message}``.
"""

import dataclasses
import re
import uuid
import zoneinfo
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.settings import TenantSettings

LINK_EXPIRY_LIMITS: dict[str, tuple[int, int]] = {
    "consent_request": (1, 30),
    "ai_screening": (1, 30),
    "client_feedback": (1, 30),
    "dsr_export": (1, 14),
}
DEFAULT_LINK_EXPIRIES: dict[str, int] = {
    "consent_request": 7,
    "ai_screening": 7,
    "client_feedback": 14,
    "dsr_export": 7,
}
# Fixed-duration links that are not tenant-configurable.
FIXED_LINK_TTLS: dict[str, timedelta] = {"tenant_export": timedelta(days=30)}
# REQ is stored by the demands domain (``demand_settings.req_prefix``); the others here.
ID_PREFIX_KEYS: tuple[str, ...] = ("CAN", "APP", "SUB", "DSR")
ALL_PREFIX_KEYS: tuple[str, ...] = ("REQ", *ID_PREFIX_KEYS)
_PREFIX_RE = re.compile(r"^[A-Z]{2,5}$")
DATE_FORMATS: tuple[str, ...] = ("DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD", "DD MMM YYYY")
NUMBER_FORMATS: tuple[str, ...] = ("indian", "international")
# ISO 4217 allow-list (active codes most tenants use; extend as needed).
CURRENCIES: frozenset[str] = frozenset(
    [
        "INR",
        "USD",
        "EUR",
        "GBP",
        "AED",
        "SGD",
        "AUD",
        "CAD",
        "JPY",
        "CNY",
        "CHF",
        "SEK",
        "NOK",
        "DKK",
        "NZD",
        "ZAR",
        "HKD",
        "MYR",
        "THB",
        "IDR",
        "PHP",
        "LKR",
        "BDT",
        "NPR",
        "SAR",
        "QAR",
        "KWD",
        "OMR",
        "BHD",
    ]
)
COMPANY_KEYS: tuple[str, ...] = (
    "display_name",
    "legal_name",
    "address",
    "website",
    "logo_file_id",
    "grievance_officer",
)


def _fields_error(fields: dict[str, str]) -> ValidationFailed:
    return ValidationFailed("Validation failed", details={"fields": fields})


async def get_row(db: AsyncSession, tenant_id: uuid.UUID | None = None) -> TenantSettings:
    row = (await db.execute(select(TenantSettings))).scalar_one_or_none()
    if row is None:
        tid = tenant_id or uuid.UUID(str(db.sync_session.info["tenant_id"]))
        row = TenantSettings(tenant_id=tid, link_expiries=dict(DEFAULT_LINK_EXPIRIES))
        db.add(row)
        await db.flush()
    return row


async def _peek(db: AsyncSession) -> TenantSettings | None:
    return (await db.execute(select(TenantSettings))).scalar_one_or_none()


# ================================================================ read helpers used by other domains
async def link_ttl(db: AsyncSession, purpose: str) -> timedelta:
    """Expiry of a secure link of ``purpose`` for the session's tenant."""
    if purpose in FIXED_LINK_TTLS:
        return FIXED_LINK_TTLS[purpose]
    row = await _peek(db)
    days = (row.link_expiries or {}).get(purpose) if row is not None else None
    if days is None:
        days = DEFAULT_LINK_EXPIRIES.get(purpose, 7)
    return timedelta(days=int(days))


async def _req_prefix(db: AsyncSession) -> str:
    """Return the REQ prefix, owned by the demands domain (``demand_settings.req_prefix``): one source of
    truth."""
    from app.services.demands import settings as demand_settings

    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    return (await demand_settings.get_demand_settings(RequestContext(tenant_id=tenant_id), db)).req_prefix


async def id_prefix(db: AsyncSession, key: str) -> str:
    if key == "REQ":
        return await _req_prefix(db)
    row = await _peek(db)
    value = (row.id_prefixes or {}).get(key) if row is not None else None
    return str(value) if value else key


async def _prefixes(db: AsyncSession, row: TenantSettings) -> dict[str, str]:
    stored = dict(row.id_prefixes or {})
    out = {k: str(stored.get(k, k)) for k in ID_PREFIX_KEYS}
    out["REQ"] = await _req_prefix(db)
    return out


# ================================================================ API
def _section(row: TenantSettings, section: str) -> dict[str, Any]:
    data = dict(getattr(row, section) or {})
    if section == "link_expiries":
        data = {**DEFAULT_LINK_EXPIRIES, **data}
    if section == "id_prefixes":
        data = {k: data.get(k, k) for k in ID_PREFIX_KEYS}
    return data


async def get_settings_sections(ctx: RequestContext, db: AsyncSession) -> dict[str, Any]:
    row = await get_row(db, ctx.tenant_id)
    from app.services import identity_bridge

    regional = {**_section(row, "regional"), **await identity_bridge.tenant_regional(db, row.tenant_id)}
    return {
        "company": _section(row, "company"),
        "regional": regional,
        "id_prefixes": await _prefixes(db, row),
        "link_expiries": _section(row, "link_expiries"),
    }


def _entry(
    category: str,
    key: str,
    label: str,
    description: str,
    endpoint: str,
    available: bool,
    summary: str | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "key": key,
        "label": label,
        "description": description,
        "endpoint": endpoint,
        "available": available,
        "summary": summary,
    }


def _route_exists(prefix: str) -> bool:
    """Whether a domain that owns a setting is built yet (its router module is importable)."""
    import importlib.util

    return importlib.util.find_spec(prefix) is not None


# ================================================================ Epic 35 settings registry (A9, FR-186)
@dataclasses.dataclass(frozen=True, slots=True)
class SettingsEntry:
    category: str
    key: str
    label: str
    description: str
    endpoint: str
    permission: str | None
    owner_epic: str | None = None


_REGISTRY: dict[str, SettingsEntry] = {}
API_PREFIX = "/api/v1"


def register_settings_entry(
    category: str,
    key: str,
    label: str,
    description: str,
    endpoint: str,
    permission: str | None,
    owner_epic: str | None = None,
) -> None:
    """Add a hub catalogue entry (owners call this from ``hooks.py``; Epic 35 registers the Phase 2 list in
    ``settings_catalog_p2``). ``available`` is computed from route existence at read time. Re-registering a
    key replaces it."""
    _REGISTRY[key] = SettingsEntry(category, key, label, description, endpoint, permission, owner_epic)


def registered_settings_entries() -> list[SettingsEntry]:
    return list(_REGISTRY.values())


def endpoint_exists(endpoint: str) -> bool:
    """Whether a route with this path is defined by a loaded ``app.router.*`` module (route existence)."""
    import sys

    path = endpoint.removeprefix(API_PREFIX) or "/"
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("app.router."):
            continue
        for obj in list(vars(module).values()):
            if type(obj).__name__ == "APIRouter":
                if any(getattr(route, "path", None) == path for route in obj.routes):
                    return True
    return False


def _registry_entries() -> list[dict[str, Any]]:
    from app.services import (
        settings_catalog_p2,  # noqa: F401 - registers the Phase 2 entries
        settings_catalog_p3,  # noqa: F401 - Epic 52: Phase 3 entries no owner registered
    )

    return [
        {
            **_entry(e.category, e.key, e.label, e.description, e.endpoint, endpoint_exists(e.endpoint)),
            "permission": e.permission,
            "owner_epic": e.owner_epic,
        }
        for e in _REGISTRY.values()
    ]


async def catalog(ctx: RequestContext, db: AsyncSession) -> list[dict[str, Any]]:
    p1 = await _p1_catalog(ctx, db)
    keys = {e["key"] for e in p1}
    return [
        *({**e, "permission": e.get("permission"), "owner_epic": e.get("owner_epic")} for e in p1),
        *(e for e in _registry_entries() if e["key"] not in keys),
    ]


async def _p1_catalog(ctx: RequestContext, db: AsyncSession) -> list[dict[str, Any]]:
    sections = await get_settings_sections(ctx, db)
    company = sections["company"]
    regional = sections["regional"]
    has_demands = _route_exists("app.router.demands")
    has_pipeline = _route_exists("app.router.pipeline")
    has_matching = _route_exists("app.router.matching")
    has_interviews = _route_exists("app.router.interviews")
    has_comms = _route_exists("app.router.comms")
    has_portal = _route_exists("app.router.portal")
    org, services, security = "organization", "services", "security_privacy"
    return [
        _entry(org, "company", "Company profile", "Name, address, logo and DPDP grievance officer.",
               "/api/v1/settings/company", True, company.get("display_name")),
        _entry(org, "regional", "Regional settings", "Time zone, currency, date and number formats.",
               "/api/v1/settings/regional", True, f"{regional.get('timezone')} · {regional.get('currency')}"),
        _entry(org, "id_prefixes", "ID prefixes", "Prefixes of human-readable IDs (REQ, CAN, APP, SUB, DSR).",
               "/api/v1/settings/id-prefixes", True, ", ".join(sections["id_prefixes"].values())),
        _entry(org, "business_hours", "Business hours and holidays", "Working hours used by SLA timers.",
               "/api/v1/settings/business-calendar", has_demands),
        _entry(services, "sla", "SLA targets", "Demand SLA targets and at-risk thresholds.",
               "/api/v1/settings/sla-targets", has_demands),
        _entry(services, "approval_chains", "Approval chains", "Demand, offer and margin approval chains.",
               "/api/v1/approval-chains", _route_exists("app.router.approvals")),
        _entry(services, "biased_terms", "Biased terms", "Terms flagged in job descriptions.",
               "/api/v1/settings/biased-terms", has_demands),
        _entry(services, "demand_settings", "Demand settings", "Demand defaults, incl. the REQ ID prefix.",
               "/api/v1/settings/demands", has_demands),
        _entry(services, "matching_weights", "Matching weights", "Weights of the deterministic match score.",
               "/api/v1/settings/matching", has_matching),
        _entry(services, "stages", "Pipeline stages", "Stage labels and custom stages.",
               "/api/v1/pipeline/stages", has_pipeline),
        _entry(services, "reasons", "Rejection and withdrawal reasons", "Reason codes.",
               "/api/v1/pipeline/reason-codes", has_pipeline),
        _entry(services, "questionnaires", "Screening questionnaires", "Recruiter screening questions.",
               "/api/v1/pipeline/questionnaires", has_pipeline),
        _entry(services, "margin", "Margin and burden", "Minimum margin % and burden % per employment type.",
               "/api/v1/pipeline/settings", has_pipeline),
        _entry(services, "interview_templates", "Interview templates and kits", "Interview stages and kits.",
               "/api/v1/interview-stage-templates", has_interviews),
        _entry(services, "email_templates", "Email templates", "Candidate and client email templates.",
               "/api/v1/email-templates", has_comms),
        _entry(services, "branding", "Career portal branding", "Logo, colours and career site texts.",
               "/api/v1/admin/career-site", has_portal),
        _entry(services, "ai_features", "AI features", "Turn each AI feature on or off.", "/api/v1/ai/features", True),
        _entry(security, "consent", "Consent notices and AI-processing basis",
               "Notice texts per purpose and the AI-processing basis.", "/api/v1/privacy/ai-basis", True),
        _entry(security, "retention", "Retention policy", "Anonymise inactive candidates after N months.",
               "/api/v1/privacy/retention/policy", True),
        _entry(security, "link_expiries", "Secure link expiries", "Days before each kind of secure link expires.",
               "/api/v1/settings/link-expiries", True,
               ", ".join(f"{k} {v}d" for k, v in sections["link_expiries"].items())),
        _entry(security, "security", "Security", "Require MFA, session timeouts.", "/api/v1/tenant/security", True),
        _entry(security, "api_keys", "API keys", "Scoped keys for integrations.", "/api/v1/api-keys", True),
    ]  # fmt: skip


def _record(db: AsyncSession, section: str, changed: list[str]) -> None:
    audit.record(
        "settings.updated", "tenant_settings", None, session=db, section=section, changed_keys=changed
    )


def _apply(row: TenantSettings, section: str, changes: dict[str, Any]) -> list[str]:
    current = dict(getattr(row, section) or {})
    changed = sorted(k for k, v in changes.items() if current.get(k) != v)
    current.update(changes)
    setattr(row, section, current)
    return changed


async def update_company(ctx: RequestContext, db: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, str] = {}
    if "logo_file_id" in data and data["logo_file_id"] is not None:
        from app.services import files_service

        file = await files_service.get_file(db, uuid.UUID(str(data["logo_file_id"])))
        if file is None or not file.content_type.startswith("image/"):
            fields["logo_file_id"] = "Must be an uploaded image file"
        else:
            data["logo_file_id"] = str(file.id)
    if fields:
        raise _fields_error(fields)
    row = await get_row(db, ctx.tenant_id)
    changed = _apply(row, "company", {k: v for k, v in data.items() if k in COMPANY_KEYS})
    await db.flush()
    _record(db, "company", changed)
    return _section(row, "company")


async def update_regional(ctx: RequestContext, db: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, str] = {}
    tz = data.get("timezone")
    if tz is not None:
        try:
            zoneinfo.ZoneInfo(str(tz))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            fields["timezone"] = "Unknown time zone (use an IANA name such as Asia/Kolkata)"
    currency = data.get("currency")
    if currency is not None and currency not in CURRENCIES:
        fields["currency"] = "Unsupported currency (ISO 4217 code expected)"
    if data.get("date_format") is not None and data["date_format"] not in DATE_FORMATS:
        fields["date_format"] = f"Must be one of {', '.join(DATE_FORMATS)}"
    if data.get("number_format") is not None and data["number_format"] not in NUMBER_FORMATS:
        fields["number_format"] = "Must be indian or international"
    if fields:
        raise _fields_error(fields)
    row = await get_row(db, ctx.tenant_id)
    local = {k: data[k] for k in ("date_format", "number_format") if data.get(k) is not None}
    changed = _apply(row, "regional", local)
    if tz is not None or currency is not None:
        from app.services import identity_bridge

        changed += await identity_bridge.set_tenant_regional(
            db, row.tenant_id, timezone=tz, currency=currency
        )
    await db.flush()
    _record(db, "regional", sorted(set(changed)))
    regional: dict[str, Any] = (await get_settings_sections(ctx, db))["regional"]
    return regional


async def update_id_prefixes(ctx: RequestContext, db: AsyncSession, data: dict[str, str]) -> dict[str, Any]:
    row = await get_row(db, ctx.tenant_id)
    merged = {**await _prefixes(db, row), **{k: v for k, v in data.items() if v is not None}}
    fields: dict[str, str] = {}
    for key, value in data.items():
        if key not in ALL_PREFIX_KEYS:
            fields[key] = "Unknown ID prefix key"
        elif value is None or not _PREFIX_RE.fullmatch(str(value)):
            fields[key] = "2 to 5 capital letters"
    seen: dict[str, str] = {}
    for key, value in merged.items():
        if value in seen:
            fields.setdefault(key, f"Already used by {seen[value]}")
        seen[value] = key
    if fields:
        raise _fields_error(fields)
    changed = _apply(row, "id_prefixes", {k: merged[k] for k in data if k != "REQ"})
    if "REQ" in data and merged["REQ"] != await _req_prefix(db):
        from app.schemas.demands import DemandSettingsIn
        from app.services.demands import settings as demand_settings

        await demand_settings.put_demand_settings(ctx, db, DemandSettingsIn(req_prefix=merged["REQ"]))
        changed.append("REQ")
    await db.flush()
    _record(db, "id_prefixes", sorted(changed))
    return await _prefixes(db, row)


async def update_link_expiries(ctx: RequestContext, db: AsyncSession, data: dict[str, int]) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for key, value in data.items():
        limits = LINK_EXPIRY_LIMITS.get(key)
        if limits is None:
            fields[key] = "Unknown link purpose"
        elif not isinstance(value, int) or not limits[0] <= value <= limits[1]:
            fields[key] = f"Must be between {limits[0]} and {limits[1]} days"
    if fields:
        raise _fields_error(fields)
    row = await get_row(db, ctx.tenant_id)
    changed = _apply(row, "link_expiries", dict(data))
    await db.flush()
    _record(db, "link_expiries", changed)
    return _section(row, "link_expiries")


async def hub(ctx: RequestContext, db: AsyncSession) -> dict[str, Any]:
    """``GET /settings``: the catalog plus the four sections owned here."""
    return {"catalog": await catalog(ctx, db), **await get_settings_sections(ctx, db)}

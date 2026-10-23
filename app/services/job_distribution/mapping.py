# ruff: file-ignore[line-too-long]
"""Careers posting -> ``JobPostingDTO`` (Epic 70, spec A3 step 3): the approved JD version of a live posting, the
posting's visibility flags honoured for every board, taxonomy mappings, then the user's board fields; required schema
fields still empty -> ``blocked_fields``. The DTO is rebuilt from the database for every push (never from a job
payload)."""

import dataclasses
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.core.settings import get_settings
from app.models.clients import Client
from app.models.demands import Demand, DemandPosting, DemandSkill, JdVersion
from app.models.identity import Tenant
from app.models.job_distribution import BoardFieldMapping
from app.models.portal import CareerSite
from app.services.connectors.job_board.port import FieldSchemaDTO, JobPostingDTO, LocationDTO, SalaryDTO
from app.services.job_distribution.common import tenant_of

BOARD_DEFAULT_DAYS = 30
SCHEMA_TTL_SECONDS = 24 * 3600
_schema_cache: dict[uuid.UUID, tuple[float, FieldSchemaDTO]] = {}


@dataclasses.dataclass(frozen=True)
class Source:
    posting: DemandPosting
    demand: Demand
    client_name: str | None
    jd: JdVersion
    skills: tuple[str, ...]
    company: str
    slug: str


async def tenant_slug_and_name(db: AsyncSession) -> tuple[str, str]:
    tid = tenant_of(db)
    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tid)
    site = await db.get(CareerSite, tid)
    name = (site.display_name if site and site.display_name else (tenant.name if tenant else "")) or ""
    return (str(tenant.slug) if tenant else ""), name


async def load_source(db: AsyncSession, posting_id: uuid.UUID) -> Source | None:
    row = (
        await db.execute(
            select(DemandPosting, Demand, Client.name)
            .join(Demand, Demand.id == DemandPosting.demand_id)
            .join(Client, Client.id == Demand.client_id)
            .where(DemandPosting.id == posting_id)
        )
    ).first()
    if row is None:
        return None
    posting, demand, client_name = row
    jd = await db.get(JdVersion, posting.jd_version_id)
    if jd is None:
        return None
    skills = (
        await db.execute(
            select(DemandSkill.skill_label)
            .where(DemandSkill.demand_id == demand.id)
            .order_by(DemandSkill.kind, DemandSkill.position, DemandSkill.skill_label)  # mandatory < preferred
        )
    ).scalars()
    slug, name = await tenant_slug_and_name(db)
    return Source(posting, demand, client_name, jd, tuple(dict.fromkeys(skills))[:30], name, slug)


def is_live(src: Source) -> bool:
    d = src.demand
    return bool(src.posting.is_live) and d.status == "open" and d.archived_at is None and d.visibility != "internal"


def careers_posting_url(slug: str, posting_id: uuid.UUID) -> str:
    s = get_settings()
    base = f"https://{slug}.{s.careers_root_domain.strip('.')}" if s.careers_root_domain else f"{s.frontend_url.rstrip('/')}/careers/{slug}"
    return f"{base}/jobs/{posting_id}"


def apply_url(slug: str, posting_id: uuid.UUID, board_key: str, board_posting_id: uuid.UUID) -> str:
    return f"{careers_posting_url(slug, posting_id)}/apply?src={board_key}&bp={board_posting_id}"


def _location(d: Demand) -> LocationDTO | None:
    parts = [p.strip() for p in (d.location or "").split(",") if p.strip()]
    return LocationDTO(city=parts[0] if parts else None, region=parts[1] if len(parts) > 1 else None, country=(d.country or "IN").upper())


def _salary(d: Demand) -> SalaryDTO | None:
    if d.pay_rate_min is None and d.pay_rate_max is None:
        return None
    return SalaryDTO(currency=d.currency or "INR", min_amount=d.pay_rate_min, max_amount=d.pay_rate_max, period=d.rate_unit or "year")  # type: ignore[arg-type]


def _expires(d: Demand, now: datetime) -> datetime:
    default = now + timedelta(days=BOARD_DEFAULT_DAYS)
    if d.target_closure_date is not None:
        closure = datetime.combine(d.target_closure_date, dtime(23, 59), tzinfo=UTC)
        if now < closure < default:
            return closure
    return default


def _band(value: Decimal | None) -> str | None:
    if value is None:
        return None
    y = int(value)
    return "0-1" if y <= 1 else "2-5" if y <= 5 else "6-10" if y <= 10 else "10+"


async def mapped_fields(db: AsyncSession, board_key: str, src: Source) -> dict[str, Any]:
    rows = list((await db.execute(select(BoardFieldMapping).where(BoardFieldMapping.board_key == board_key))).scalars())
    d = src.demand
    lookups = {
        "function": (d.department or "").strip(),
        "location": (loc.city or "") if src.posting.show_location and (loc := _location(d)) else "",
        "experience_band": _band(d.min_experience_years) or "",
        "employment_type": d.employment_type or "",
        "education": (d.education or "").strip(),
    }
    field_for_kind = {"function": "functional_area", "industry": "industry", "location": "location", "experience_band": "experience_band", "salary_band": "salary_band", "employment_type": "employment_type", "education": "education"}
    out: dict[str, Any] = {}
    for m in rows:
        if m.kind == "default":
            out.setdefault(m.local_value, m.board_value)
            continue
        local = lookups.get(m.kind)
        if local and local.lower() == m.local_value.lower():
            out[field_for_kind.get(m.kind, m.kind)] = m.board_value
    return out


async def field_schema(adapter: Any, installation_id: uuid.UUID) -> FieldSchemaDTO:
    hit = _schema_cache.get(installation_id)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    try:
        schema = await adapter.field_schema()
    except Exception:  # a board schema problem never blocks the preview of other boards
        schema = FieldSchemaDTO(fields=())
    _schema_cache[installation_id] = (time.monotonic() + SCHEMA_TTL_SECONDS, schema)
    return schema  # type: ignore[no-any-return]


def clear_schema_cache() -> None:
    _schema_cache.clear()


def blocked(schema: FieldSchemaDTO, fields: dict[str, Any]) -> list[str]:
    return [f.key for f in schema.fields if f.required and fields.get(f.key) in (None, "", [])]


def to_dto(src: Source, board_key: str, board_posting_id: uuid.UUID, revision: int, fields: dict[str, Any], *, now: datetime | None = None) -> JobPostingDTO:
    from app.services.portal.common import render_markdown

    d, p = src.demand, src.posting
    openings = max(int(d.positions or 1) - int(d.positions_filled or 0), 1)
    return JobPostingDTO(
        posting_id=board_posting_id,
        demand_id=d.id,
        revision=revision,
        title=d.title,
        description_html=render_markdown(src.jd.content),
        location=_location(d) if p.show_location else None,
        work_mode=(d.work_mode or "onsite"),  # type: ignore[arg-type]
        employment_type=d.employment_type or "permanent",
        experience_min_years=d.min_experience_years,
        experience_max_years=d.max_experience_years,
        salary=_salary(d) if p.show_rate else None,
        skills=src.skills,
        openings=openings,
        company_name=(src.client_name if p.show_client_name and src.client_name else src.company) or None,
        apply_url=apply_url(src.slug, p.id, board_key, board_posting_id),
        expires_at=_expires(d, now or datetime.now(UTC)),
        board_fields={**fields, "requisition_id": d.req_id},
    )


def content_sha256(dto: JobPostingDTO) -> str:
    """Hash of the mapped DTO minus ``board_fields`` / ``revision`` / ``expires_at`` (what the board shows)."""
    data = dataclasses.asdict(dto)
    for k in ("board_fields", "revision", "expires_at", "posting_id", "apply_url"):
        data.pop(k, None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()

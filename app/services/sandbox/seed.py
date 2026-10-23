"""Deterministic synthetic seed for Sandbox Tenants (Epic 66, Story 66.9, FR-339, NFR-68).

``build_plan(size, seed)`` is a pure function of ``(size, seed)`` (``random.Random(seed)``): the same inputs give the
same plan, and ``plan_digest`` hashes it canonically. ``apply_plan`` writes it into a tenant (models directly, inside one
tenant RLS session; human ids come from the tenant sequences, so a fresh tenant gets identical human ids), and
``content_digest`` reads the tenant back into the same canonical form (ids and timestamps excluded) so two tenants seeded
from the same seed compare equal.

Synthetic-data rules:

* names come from the fixed lists below (``FIRST_NAMES`` x ``LAST_NAMES``; company names are invented);
* every email is ``<local>@example.test`` (RFC 2606/6761 reserved TLD ``.test`` -- never deliverable);
* every phone is ``+91 0000 NNNNNN`` (E.164 ``+910000NNNNNN``). An Indian national significant number never starts
  with ``0`` (``0`` is the domestic trunk prefix and mobile numbers start with 6-9), so these numbers are unallocated
  and unroutable by construction. ``is_reserved_phone`` / ``is_reserved_email`` check both rules.

Entities: Clients (+1 Contact each), Demands (open, internal visibility so they never reach a public careers page),
Candidates (+ primary email/phone), Applications (default pipeline stages), Placements (from ``hired`` applications, the
tenant's default Legal Entity), Timesheets (draft/submitted weeks with daily regular-hour entries) and Invoices (draft
headers with totals; no lines, series or numbers -- issuing goes through billing, which needs GST registrations).
"""

import dataclasses
import hashlib
import json
import random
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sequences import next_human_id

Size = Literal["small", "medium"]

SIZES: dict[str, dict[str, int]] = {
    "small": {
        "clients": 5,
        "demands": 10,
        "candidates": 50,
        "applications": 80,
        "placements": 5,
        "timesheets": 10,
        "invoices": 5,
    },
    "medium": {
        "clients": 25,
        "demands": 50,
        "candidates": 250,
        "applications": 400,
        "placements": 25,
        "timesheets": 50,
        "invoices": 25,
    },
}

EMAIL_DOMAIN = "example.test"
PHONE_PREFIX = "+910000"  # + 6 digits -> +91 0000 NNNNNN (NSN starting with 0: unallocated in India)
SEED_OWNER_EMAIL = f"seed.owner@sandbox.{EMAIL_DOMAIN}"
SEED_OWNER_NAME = "Sandbox Seed Owner"

FIRST_NAMES = (
    "Aarav", "Bhavna", "Chetan", "Divya", "Eshan", "Farah", "Gautam", "Hema", "Ishaan", "Jaya",
    "Kabir", "Lavanya", "Manav", "Nandini", "Omkar", "Pallavi", "Qadir", "Rhea", "Sameer", "Tara",
    "Uday", "Vidya", "Waseem", "Yamini", "Zubin",
)  # fmt: skip
LAST_NAMES = (
    "Acharya", "Bose", "Chandran", "Dutta", "Eswaran", "Fernandes", "Ghosh", "Hegde", "Iyer", "Jain",
    "Kulkarni", "Lobo", "Menon", "Nair", "Oberoi", "Pillai", "Rao", "Sethi", "Thakur", "Varma",
)  # fmt: skip
COMPANY_STEMS = (
    "Asterbyte", "Bluefinch", "Cobaltway", "Dunewell", "Emberlake", "Fernlight", "Glasswing", "Harborpine",
    "Ironmoss", "Juniperline", "Kestrelgate", "Lumenfield", "Marigold Peak", "Northquill", "Opaline",
    "Pebblecrest", "Quartzhill", "Riverlark", "Saffronbay", "Tealstone", "Umberfox", "Velvetoak",
    "Willowmere", "Xenonbridge", "Yarrowdale", "Zephyrcove",
)  # fmt: skip
COMPANY_SUFFIXES = ("Systems", "Labs", "Technologies", "Retail", "Logistics", "Health", "Finserv")
INDUSTRIES = ("IT Services", "Retail", "Logistics", "Healthcare", "Banking", "Manufacturing")
CITIES = ("Bengaluru", "Pune", "Chennai", "Hyderabad", "Gurugram", "Kochi", "Ahmedabad")
TITLES = (
    "Java Developer", "Data Engineer", "QA Engineer", "DevOps Engineer", "Business Analyst",
    "Frontend Developer", "Data Analyst", "Project Manager", "Support Engineer", "Python Developer",
)  # fmt: skip
EMPLOYMENT_TYPES = ("contract", "contract_to_hire", "permanent")
WORK_MODES = ("onsite", "hybrid", "remote")
PRIORITIES = ("low", "medium", "high")
# Non-terminal + terminal stage mix for the applications that are not placements.
STAGE_MIX = (
    "applied", "applied", "applied", "screening", "screening", "shortlisted", "shortlisted",
    "submitted", "interviewing", "selected", "rejected", "withdrawn",
)  # fmt: skip

_EMAIL_RE = re.compile(r"^[a-z0-9._+-]+@([a-z0-9-]+\.)*example\.test$")
_PHONE_RE = re.compile(r"^\+910000\d{6}$")


def is_reserved_email(value: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(value.lower()))


def is_reserved_phone(value: str) -> bool:
    return bool(_PHONE_RE.fullmatch(value))


# ================================================================ plan (pure)
@dataclasses.dataclass(frozen=True, slots=True)
class SeedPlan:
    size: str
    seed: int
    clients: list[dict[str, Any]]
    demands: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    applications: list[dict[str, Any]]
    placements: list[dict[str, Any]]
    timesheets: list[dict[str, Any]]
    invoices: list[dict[str, Any]]

    def counts(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in SIZES["small"]}

    def canonical(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in SIZES["small"]}


def _phone(n: int) -> str:
    return f"{PHONE_PREFIX}{n:06d}"


def build_plan(size: Size, seed: int) -> SeedPlan:
    """Pure: same ``(size, seed)`` -> identical plan. References between entities are list indexes."""
    if size not in SIZES:
        msg = f"unknown sandbox size {size!r}"
        raise ValueError(msg)
    n = SIZES[size]
    rng = random.Random(seed)

    stems = rng.sample(COMPANY_STEMS, k=min(n["clients"], len(COMPANY_STEMS)))
    clients: list[dict[str, Any]] = []
    for i in range(n["clients"]):
        stem = stems[i % len(stems)]
        name = f"{stem} {rng.choice(COMPANY_SUFFIXES)}" + (
            "" if i < len(stems) else f" {i // len(stems) + 1}"
        )
        cfirst, clast = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        slug = re.sub(r"[^a-z0-9]+", "", stem.lower())
        clients.append(
            {
                "name": name,
                "industry": rng.choice(INDUSTRIES),
                "city": rng.choice(CITIES),
                "contact": {
                    "name": f"{cfirst} {clast}",
                    "title": "Hiring Manager",
                    "email": f"{cfirst.lower()}.{clast.lower()}.{i + 1}@{slug}.{EMAIL_DOMAIN}",
                    "phone": _phone(900000 + i),
                },
            }
        )

    demands: list[dict[str, Any]] = []
    for _i in range(n["demands"]):
        title = rng.choice(TITLES)
        lo = rng.randint(1, 6)
        rate = rng.randrange(800, 3000, 50)
        demands.append(
            {
                "client": rng.randrange(n["clients"]),
                "title": title,
                "positions": rng.randint(1, 3),
                "employment_type": rng.choice(EMPLOYMENT_TYPES),
                "work_mode": rng.choice(WORK_MODES),
                "priority": rng.choice(PRIORITIES),
                "location": rng.choice(CITIES),
                "min_exp": lo,
                "max_exp": lo + rng.randint(2, 6),
                "bill_rate": rate,
                "start_offset_days": rng.randint(7, 45),
            }
        )

    candidates: list[dict[str, Any]] = []
    for i in range(n["candidates"]):
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        months = rng.randint(6, 180)
        candidates.append(
            {
                "full_name": f"{first} {last}",
                "email": f"{first.lower()}.{last.lower()}.{i + 1}@{EMAIL_DOMAIN}",
                "phone": _phone(i + 1),
                "city": rng.choice(CITIES),
                "current_title": rng.choice(TITLES),
                "current_employer": f"{rng.choice(COMPANY_STEMS)} {rng.choice(COMPANY_SUFFIXES)}",
                "experience_months": months,
                "notice_days": rng.choice((0, 15, 30, 60, 90)),
            }
        )

    # Unique (candidate, demand) pairs; the first ``placements`` become hired applications.
    all_pairs = n["candidates"] * n["demands"]
    picks = rng.sample(range(all_pairs), k=n["applications"])
    applications: list[dict[str, Any]] = []
    for j, p in enumerate(picks):
        cand, dem = divmod(p, n["demands"])
        stage = "hired" if j < n["placements"] else rng.choice(STAGE_MIX)
        applications.append(
            {"candidate": cand, "demand": dem, "stage": stage, "age_days": rng.randint(1, 60)}
        )

    placements: list[dict[str, Any]] = []
    for j in range(n["placements"]):
        app = applications[j]
        dem = demands[app["demand"]]
        placements.append(
            {
                "application": j,
                "candidate": app["candidate"],
                "demand": app["demand"],
                "client": dem["client"],
                "start_offset_days": -rng.randint(28, 120),
                "bill_rate": dem["bill_rate"],
            }
        )

    timesheets: list[dict[str, Any]] = []
    per = max(1, n["timesheets"] // max(1, n["placements"]))
    for t in range(n["timesheets"]):
        pl = t % n["placements"]
        week = t // n["placements"] if per else 0
        hours = [rng.choice((8, 8, 8, 7, 9)) for _ in range(5)]
        timesheets.append(
            {"placement": pl, "week": week, "status": "submitted" if week == 0 else "draft", "hours": hours}
        )

    invoices: list[dict[str, Any]] = []
    for k in range(n["invoices"]):
        pl = k % n["placements"]
        hours = sum(sum(t["hours"]) for t in timesheets if t["placement"] == pl) or 40
        rate = placements[pl]["bill_rate"]
        invoices.append({"placement": pl, "client": placements[pl]["client"], "hours": hours, "rate": rate})

    return SeedPlan(
        size=size,
        seed=seed,
        clients=clients,
        demands=demands,
        candidates=candidates,
        applications=applications,
        placements=placements,
        timesheets=timesheets,
        invoices=invoices,
    )


def _digest(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def plan_digest(plan: SeedPlan) -> str:
    return _digest(plan.canonical())


# ================================================================ apply (writes)
@dataclasses.dataclass(frozen=True, slots=True)
class SeedResult:
    size: str
    seed: int
    counts: dict[str, int]
    plan_digest: str
    anchor: date


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def _seed_owner(db: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """An inactive, password-less internal user that owns the seeded records (FK target for ``owner_id``)."""
    from app.models.identity import User

    existing = (await db.execute(select(User.id).where(User.email == SEED_OWNER_EMAIL))).scalar_one_or_none()
    if existing is not None:
        return existing
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email=SEED_OWNER_EMAIL,
        name=SEED_OWNER_NAME,
        password_hash="!sandbox-seed-no-login",
        is_active=False,
    )
    db.add(user)
    await db.flush()
    return user.id


async def apply_plan(
    db: AsyncSession, tenant_id: uuid.UUID, plan: SeedPlan, *, anchor: date | None = None
) -> SeedResult:
    """Write ``plan`` into the session's tenant (``db`` is a tenant RLS session). ``anchor`` (default today, UTC) is
    the Monday-based reference for every date."""
    from app.models.billing import Invoice
    from app.models.candidates import Candidate, CandidateEmail, CandidatePhone
    from app.models.clients import Client, Contact
    from app.models.demands import Demand
    from app.models.legal_entities import LegalEntity
    from app.models.pipeline import Application, PipelineStage
    from app.models.placements import Placement
    from app.models.time import Timesheet, TimesheetEntry
    from app.services import settings_service
    from app.services.pipeline import ensure_default_stages

    base = _week_start(anchor or datetime.now(UTC).date())
    now = datetime.now(UTC)
    owner = await _seed_owner(db, tenant_id)
    le_id = (
        await db.execute(select(LegalEntity.id).order_by(LegalEntity.created_at, LegalEntity.id).limit(1))
    ).scalar_one_or_none()

    # ---- clients + contacts
    client_ids: list[uuid.UUID] = []
    for c in plan.clients:
        cid = uuid.uuid4()
        db.add(
            Client(
                id=cid,
                tenant_id=tenant_id,
                name=c["name"],
                industry=c["industry"],
                locations=[{"city": c["city"], "state": None, "country": "IN"}],
                owner_id=owner,
                status="active",
            )
        )
        client_ids.append(cid)
    await db.flush()
    for cid, c in zip(client_ids, plan.clients, strict=True):
        ct = c["contact"]
        db.add(
            Contact(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                client_id=cid,
                name=ct["name"],
                title=ct["title"],
                email=ct["email"],
                phone=ct["phone"],
                type="hiring_manager",
            )
        )

    # ---- demands
    req_prefix = await settings_service.id_prefix(db, "REQ")
    demand_ids: list[uuid.UUID] = []
    app_counts = [0] * len(plan.demands)
    for a in plan.applications:
        app_counts[a["demand"]] += 1
    for i, d in enumerate(plan.demands):
        did = uuid.uuid4()
        db.add(
            Demand(
                id=did,
                tenant_id=tenant_id,
                req_id=await next_human_id(db, "REQ", prefix=req_prefix, width=4),
                title=d["title"],
                client_id=client_ids[d["client"]],
                positions=d["positions"],
                employment_type=d["employment_type"],
                country="IN",
                location=d["location"],
                work_mode=d["work_mode"],
                min_experience_years=Decimal(d["min_exp"]),
                max_experience_years=Decimal(d["max_exp"]),
                priority=d["priority"],
                bill_rate=Decimal(d["bill_rate"]),
                currency="INR",
                rate_unit="hour",
                start_date=base + timedelta(days=d["start_offset_days"]),
                status="open",
                opened_at=now,
                owner_id=owner,
                visibility="internal",
                applications_count=app_counts[i],
            )
        )
        demand_ids.append(did)
    await db.flush()

    # ---- candidates
    can_prefix = await settings_service.id_prefix(db, "CAN")
    candidate_ids: list[uuid.UUID] = []
    for c in plan.candidates:
        cid = uuid.uuid4()
        db.add(
            Candidate(
                id=cid,
                tenant_id=tenant_id,
                human_id=await next_human_id(db, "CAN", prefix=can_prefix, width=6),
                full_name=c["full_name"],
                location={"city": c["city"], "country": "India"},
                current_title=c["current_title"],
                current_employer=c["current_employer"],
                total_experience_months=c["experience_months"],
                notice_period_days=c["notice_days"],
                source="manual",
                source_detail="sandbox_seed",
                owner_id=owner,
            )
        )
        candidate_ids.append(cid)
    await db.flush()
    for cid, c in zip(candidate_ids, plan.candidates, strict=True):
        db.add(
            CandidateEmail(
                id=uuid.uuid4(), tenant_id=tenant_id, candidate_id=cid, email=c["email"], is_primary=True
            )
        )
        db.add(
            CandidatePhone(
                id=uuid.uuid4(), tenant_id=tenant_id, candidate_id=cid, phone_e164=c["phone"], is_primary=True
            )
        )

    # ---- applications
    await ensure_default_stages(db, tenant_id)
    await db.flush()
    stages = {k: v for k, v in (await db.execute(select(PipelineStage.key, PipelineStage.id))).all()}
    app_prefix = await settings_service.id_prefix(db, "APP")
    application_ids: list[uuid.UUID] = []
    for a in plan.applications:
        aid = uuid.uuid4()
        db.add(
            Application(
                id=aid,
                tenant_id=tenant_id,
                human_id=await next_human_id(db, "APP", app_prefix, width=6),
                candidate_id=candidate_ids[a["candidate"]],
                demand_id=demand_ids[a["demand"]],
                stage_id=stages[a["stage"]],
                stage_entered_at=now - timedelta(days=a["age_days"]),
                source="manual",
                source_ref="sandbox_seed",
                owner_id=owner,
                screening_passed=a["stage"] not in ("applied", "screening", "rejected", "withdrawn"),
            )
        )
        application_ids.append(aid)
    await db.flush()

    # ---- placements, timesheets, invoices (need the default Legal Entity)
    placement_ids: list[uuid.UUID] = []
    if le_id is not None:
        for p in plan.placements:
            pid = uuid.uuid4()
            db.add(
                Placement(
                    id=pid,
                    tenant_id=tenant_id,
                    human_id=await next_human_id(db, "placement", "PLC"),
                    candidate_id=candidate_ids[p["candidate"]],
                    client_id=client_ids[p["client"]],
                    application_id=application_ids[p["application"]],
                    demand_id=demand_ids[p["demand"]],
                    legal_entity_id=le_id,
                    worker_type="contractor",
                    payee_kind="worker",
                    engagement_kind="contract",
                    status="active",
                    planned_start_date=base + timedelta(days=p["start_offset_days"]),
                    actual_start_date=base + timedelta(days=p["start_offset_days"]),
                    currency="INR",
                    account_manager_user_id=owner,
                    recruiter_user_id=owner,
                )
            )
            placement_ids.append(pid)
        await db.flush()

        for t in plan.timesheets:
            p = plan.placements[t["placement"]]
            start = base - timedelta(days=7 * (t["week"] + 1))
            tid = uuid.uuid4()
            sheet = Timesheet(
                id=tid,
                tenant_id=tenant_id,
                human_id=await next_human_id(db, "timesheet", "TS", width=6),
                placement_id=placement_ids[t["placement"]],
                candidate_id=candidate_ids[p["candidate"]],
                client_id=client_ids[p["client"]],
                legal_entity_id=le_id,
                period_start=start,
                period_end=start + timedelta(days=6),
                status="draft",  # entries are immutable once submitted: add them first
            )
            db.add(sheet)
            await db.flush()
            for day, hours in enumerate(t["hours"]):
                db.add(
                    TimesheetEntry(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        timesheet_id=tid,
                        entry_date=start + timedelta(days=day),
                        category="regular",
                        hours=Decimal(hours),
                    )
                )
            await db.flush()
            if t["status"] == "submitted":
                sheet.status, sheet.submitted_at = "submitted", now
                sheet.submitted_by_kind, sheet.submitted_by_id = "user", owner
        await db.flush()

        for inv in plan.invoices:
            total = Decimal(inv["hours"]) * Decimal(inv["rate"])
            db.add(
                Invoice(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    legal_entity_id=le_id,
                    client_id=client_ids[inv["client"]],
                    currency="INR",
                    status="draft",
                    taxable_total=total,
                    document_total=total,
                    inr_taxable_total=total,
                    inr_document_total=total,
                )
            )
        await db.flush()

    # Search projection (best effort: the seed is still valid without it; ``search reindex`` rebuilds it).
    try:
        from app.services.search import projection

        async with db.begin_nested():
            await projection.refresh_docs(db, tenant_id, candidate_ids)
    except Exception:
        from loguru import logger

        logger.warning("Sandbox seed: search projection refresh failed (tenant_id={})", tenant_id)

    counts = plan.counts()
    if le_id is None:
        counts.update(placements=0, timesheets=0, invoices=0)
    return SeedResult(
        size=plan.size, seed=plan.seed, counts=counts, plan_digest=plan_digest(plan), anchor=base
    )


async def seed_tenant(
    db: AsyncSession, tenant_id: uuid.UUID, *, size: Size, seed: int, anchor: date | None = None
) -> SeedResult:
    return await apply_plan(db, tenant_id, build_plan(size, seed), anchor=anchor)


# ================================================================ read back
async def content_digest(db: AsyncSession) -> str:
    """Canonical digest of the seeded content of the session's tenant (no ids, no timestamps; dates kept)."""
    rows = await read_content(db)
    return _digest(rows)


async def read_content(db: AsyncSession) -> dict[str, Any]:
    from app.models.billing import Invoice
    from app.models.candidates import Candidate, CandidateEmail, CandidatePhone
    from app.models.clients import Client, Contact
    from app.models.demands import Demand
    from app.models.pipeline import Application, PipelineStage
    from app.models.placements import Placement
    from app.models.time import Timesheet, TimesheetEntry

    clients = {r.id: r for r in (await db.execute(select(Client))).scalars()}
    cname = {k: str(v.name) for k, v in clients.items()}
    contacts = sorted(
        (cname[c.client_id], c.name, c.email, c.phone) for c in (await db.execute(select(Contact))).scalars()
    )
    demands = {r.id: r for r in (await db.execute(select(Demand))).scalars()}
    dkey = {k: v.req_id for k, v in demands.items()}
    cands = {r.id: r for r in (await db.execute(select(Candidate))).scalars()}
    ckey = {k: v.human_id for k, v in cands.items()}
    emails = {r.candidate_id: r.email for r in (await db.execute(select(CandidateEmail))).scalars()}
    phones = {r.candidate_id: r.phone_e164 for r in (await db.execute(select(CandidatePhone))).scalars()}
    stages = {k: v for k, v in (await db.execute(select(PipelineStage.id, PipelineStage.key))).all()}
    apps = list((await db.execute(select(Application))).scalars())
    akey = {a.id: a.human_id for a in apps}
    placements = {r.id: r for r in (await db.execute(select(Placement))).scalars()}
    pkey = {k: v.human_id for k, v in placements.items()}
    sheets = list((await db.execute(select(Timesheet))).scalars())
    hours: dict[uuid.UUID, list[str]] = {}
    for e in (await db.execute(select(TimesheetEntry).order_by(TimesheetEntry.entry_date))).scalars():
        hours.setdefault(e.timesheet_id, []).append(f"{e.entry_date}:{e.category}:{e.hours}")
    invoices = list((await db.execute(select(Invoice))).scalars())

    return {
        "clients": sorted(
            (str(c.name), c.industry, json.dumps(c.locations, sort_keys=True), c.status)
            for c in clients.values()
        ),
        "contacts": contacts,
        "demands": sorted(
            (
                d.req_id,
                d.title,
                cname[d.client_id],
                d.positions,
                d.employment_type,
                d.work_mode,
                d.priority,
                d.location,
                str(d.min_experience_years),
                str(d.max_experience_years),
                str(d.bill_rate),
                str(d.start_date),
                d.applications_count,
            )
            for d in demands.values()
        ),
        "candidates": sorted(
            (
                c.human_id,
                c.full_name,
                json.dumps(c.location, sort_keys=True),
                c.current_title,
                c.current_employer,
                c.total_experience_months,
                c.notice_period_days,
                emails.get(c.id),
                phones.get(c.id),
            )
            for c in cands.values()
        ),
        "applications": sorted(
            (a.human_id, ckey[a.candidate_id], dkey[a.demand_id], stages[a.stage_id]) for a in apps
        ),
        "placements": sorted(
            (
                p.human_id,
                ckey[p.candidate_id],
                cname[p.client_id],
                akey.get(p.application_id),
                dkey.get(p.demand_id),
                str(p.planned_start_date),
                p.status,
            )
            for p in placements.values()
        ),
        "timesheets": sorted(
            (
                t.human_id,
                pkey[t.placement_id],
                str(t.period_start),
                str(t.period_end),
                t.status,
                hours.get(t.id, []),
            )
            for t in sheets
        ),
        "invoices": sorted(
            (cname[i.client_id], str(i.document_total), i.status, i.currency) for i in invoices
        ),
    }


async def synthetic_contact_points(db: AsyncSession) -> tuple[list[str], list[str]]:
    """Every email and phone the seed wrote (candidates, contacts, the seed owner) -- for the reserved-range checks."""
    from app.models.candidates import CandidateEmail, CandidatePhone
    from app.models.clients import Contact
    from app.models.identity import User

    emails = [str(e) for e in (await db.execute(select(CandidateEmail.email))).scalars()]
    emails += [str(e) for e in (await db.execute(select(Contact.email))).scalars() if e]
    emails += [
        str(e) for e in (await db.execute(select(User.email).where(User.email == SEED_OWNER_EMAIL))).scalars()
    ]
    phones = [str(p) for p in (await db.execute(select(CandidatePhone.phone_e164))).scalars()]
    phones += [str(p) for p in (await db.execute(select(Contact.phone))).scalars() if p]
    return emails, phones

# ruff: noqa: E501, D401, D400, D415
"""Deterministic duplicate scoring (Epic 55, spec 0.5, FR-283). The model only explains; it never scores.

``score(a, b) -> SimilarityOut(score in [0, 1], reasons)`` over two ``DemandFacts``:

| Component | Weight | Rule |
|---|---|---|
| Title | 0.30 | trigram similarity of the lower-cased titles (the pg_trgm ``similarity`` algorithm, in Python) |
| Skills | 0.30 | Jaccard over resolved mandatory skill ids (labels when unresolved) |
| Embedding | 0.15 | cosine of the demand vectors when both exist; else its weight is redistributed proportionally |
| Location / work mode | 0.10 | same city (normalised) 0.07 + same work mode 0.03 |
| Hiring contact | 0.10 | same ``hiring_manager_contact_id`` |
| Dates | 0.05 | start dates within 30 days, or both null |

``find(ctx, db, demand_id)`` compares a demand with the **same Client's** demands that are draft / pending approval /
open / on hold, or closed / cancelled within ``duplicate_window_days`` (by ``updated_at``), visible to the caller.
"""

import dataclasses
import math
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.schemas.demand_insights import ReasonOut, SimilarityOut

WEIGHTS: dict[str, Decimal] = {
    "title": Decimal("0.30"),
    "skills": Decimal("0.30"),
    "embedding": Decimal("0.15"),
    "location": Decimal("0.10"),
    "contact": Decimal("0.10"),
    "dates": Decimal("0.05"),
}
ACTIVE_STATUSES = ("draft", "pending_approval", "open", "on_hold")
CLOSED_STATUSES = ("closed", "cancelled")
_WORD = re.compile(r"[a-z0-9]+")
_CITY_ALIASES = {
    "bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata",
    "poona": "pune", "trivandrum": "thiruvananthapuram", "cochin": "kochi", "baroda": "vadodara", "new delhi": "delhi",
    "ncr": "delhi",
}  # fmt: skip


@dataclasses.dataclass(frozen=True, slots=True)
class DemandFacts:
    id: uuid.UUID
    title: str
    mandatory_skills: frozenset[str]  # "global:<id>" / "tenant:<id>" or "label:<lower label>"
    location: str | None = None
    work_mode: str | None = None
    contact_id: uuid.UUID | None = None
    start_date: date | None = None
    vector: tuple[float, ...] | None = None
    human_id: str | None = None
    status: str | None = None
    owner_id: uuid.UUID | None = None
    skill_names: tuple[str, ...] = ()


def trigrams(text: str) -> set[str]:
    out: set[str] = set()
    for word in _WORD.findall((text or "").lower()):
        padded = f"  {word} "
        out.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return out


def trigram_similarity(a: str, b: str) -> float:
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def city(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split(",")[0].strip().lower()
    first = re.sub(r"\s+", " ", first)
    first = re.sub(r"\b(hybrid|remote|onsite|on-site|office)\b", "", first).strip(" -/()")
    return _CITY_ALIASES.get(first, first) or None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _q(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def score(a: DemandFacts, b: DemandFacts) -> SimilarityOut:
    """Weighted similarity in [0, 1] with field-level reasons (deterministic)."""
    parts: dict[str, float] = {}
    reasons: list[ReasonOut] = []
    title = trigram_similarity(a.title, b.title)
    parts["title"] = title
    if title >= 0.999:
        reasons.append(ReasonOut(field="title", detail="same title"))
    elif title >= 0.5:
        reasons.append(ReasonOut(field="title", detail=f"similar title ({round(title * 100)}%)"))
    union = a.mandatory_skills | b.mandatory_skills
    common = a.mandatory_skills & b.mandatory_skills
    skills = len(common) / len(union) if union else 0.0
    parts["skills"] = skills
    if common:
        reasons.append(
            ReasonOut(
                field="skills",
                detail=f"{len(common)} of {max(len(a.mandatory_skills), len(b.mandatory_skills))} skills",
            )
        )
    has_vectors = a.vector is not None and b.vector is not None
    if has_vectors:
        parts["embedding"] = cosine(a.vector or (), b.vector or ())
    same_city = city(a.location) is not None and city(a.location) == city(b.location)
    same_mode = a.work_mode is not None and a.work_mode == b.work_mode
    parts["location"] = (0.7 if same_city else 0.0) + (0.3 if same_mode else 0.0)
    if same_city:
        reasons.append(ReasonOut(field="location", detail="same city"))
    if same_mode:
        reasons.append(ReasonOut(field="work_mode", detail="same work mode"))
    same_contact = a.contact_id is not None and a.contact_id == b.contact_id
    parts["contact"] = 1.0 if same_contact else 0.0
    if same_contact:
        reasons.append(ReasonOut(field="hiring_contact", detail="same hiring contact"))
    if a.start_date is None and b.start_date is None:
        parts["dates"] = 1.0
    elif (
        a.start_date is not None
        and b.start_date is not None
        and abs((a.start_date - b.start_date).days) <= 30
    ):
        parts["dates"] = 1.0
        reasons.append(ReasonOut(field="start_date", detail="start dates within 30 days"))
    else:
        parts["dates"] = 0.0
    weights = dict(WEIGHTS)
    if not has_vectors:
        weights.pop("embedding")
    total_weight = sum(weights.values())
    raw = sum(Decimal(str(parts[k])) * w for k, w in weights.items()) / total_weight
    value = min(Decimal(1), max(Decimal(0), raw))
    return SimilarityOut(score=_q(value), reasons=reasons[:8])


def summary_text(other_human_id: str | None, reasons: Sequence[ReasonOut]) -> str:
    """The banner sentence: "REQ-1041 looks similar: same title, 4 of 5 skills, same hiring contact." """
    details = ", ".join(r.detail for r in reasons) or "several fields match"
    return f"{other_human_id or 'Another demand'} looks similar: {details}."


# ================================================================ DB
async def facts_for(
    db: Any, demands: Sequence[Any], *, with_vectors: bool = True
) -> dict[uuid.UUID, DemandFacts]:
    from sqlalchemy import select

    from app.models.demands import DemandSkill

    ids = [d.id for d in demands]
    skills: dict[uuid.UUID, set[str]] = {i: set() for i in ids}
    names: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    if ids:
        rows = (
            await db.execute(
                select(DemandSkill).where(DemandSkill.demand_id.in_(ids)).order_by(DemandSkill.position)
            )
        ).scalars()
        for r in rows:
            if r.kind != "mandatory":
                continue
            key = (
                f"{r.skill_ref_kind}:{r.skill_id}"
                if r.skill_id and r.skill_ref_kind
                else f"label:{r.skill_label.strip().lower()}"
            )
            skills[r.demand_id].add(key)
            names[r.demand_id].append(r.skill_label)
    vectors: dict[uuid.UUID, list[float]] = {}
    if with_vectors and ids:
        try:
            from app.services.ai.vector_index import get_vector_index

            index = await get_vector_index(db)
            vectors = await index.vectors(db, "demand", ids)
        except Exception:  # vectors are optional (weight redistributed)
            vectors = {}
    out: dict[uuid.UUID, DemandFacts] = {}
    for d in demands:
        vec = vectors.get(d.id)
        out[d.id] = DemandFacts(
            id=d.id, title=d.title or "", mandatory_skills=frozenset(skills.get(d.id, set())), location=d.location,
            work_mode=d.work_mode, contact_id=d.hiring_manager_contact_id, start_date=d.start_date,
            vector=tuple(vec) if vec else None, human_id=d.req_id, status=d.status, owner_id=d.owner_id,
            skill_names=tuple(names.get(d.id, [])),
        )  # fmt: skip
    return out


async def candidates_stmt(principal: Any, demand: Any, window_days: int) -> Any:
    from sqlalchemy import and_, or_, select

    from app.models.demands import Demand
    from app.services.demands.common import READ, access_condition

    since = datetime.now(UTC) - timedelta(days=window_days)
    stmt = select(Demand).where(
        Demand.client_id == demand.client_id,
        Demand.id != demand.id,
        Demand.archived_at.is_(None),
        or_(
            Demand.status.in_(ACTIVE_STATUSES),
            and_(Demand.status.in_(CLOSED_STATUSES), Demand.updated_at >= since),
        ),
    )
    cond = access_condition(principal, READ)
    if cond is not None:
        stmt = stmt.where(cond)
    return stmt.order_by(Demand.created_at.desc()).limit(200)


async def find(
    ctx: Any, db: Any, demand_id: uuid.UUID, *, limit: int = 5, floor: Decimal | None = None
) -> list[tuple[DemandFacts, SimilarityOut]]:
    """Scored same-client candidates above ``floor`` (default ``0.5 x duplicate_threshold``), best first; only demands
    readable by ``ctx``."""
    from app.services.demand_intake import settings as settings_mod
    from app.services.demands.common import READ, get_for, principal_of

    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    s = await settings_mod.row(db)
    floor = floor if floor is not None else Decimal(str(s.duplicate_threshold)) * Decimal("0.5")
    others = list(
        (
            await db.execute(await candidates_stmt(principal_of(ctx), demand, s.duplicate_window_days))
        ).scalars()
    )
    facts = await facts_for(db, [demand, *others])
    base = facts[demand.id]
    scored = [(facts[o.id], score(base, facts[o.id])) for o in others]
    kept = [x for x in scored if x[1].score >= floor]
    kept.sort(key=lambda x: (-x[1].score, str(x[0].id)))
    return kept[:limit]


__all__ = [
    "ACTIVE_STATUSES",
    "WEIGHTS",
    "DemandFacts",
    "candidates_stmt",
    "city",
    "cosine",
    "facts_for",
    "find",
    "score",
    "summary_text",
    "trigram_similarity",
    "trigrams",
]

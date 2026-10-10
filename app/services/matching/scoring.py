"""Deterministic match scoring (Epic 5, FR-30, AD-8, NFR-14). Pure and synchronous: no DB, no AI imports.

Inputs are the frozen ``DemandMatchProfile`` / ``CandidateMatchProfile`` (structured fields only -- no
resume text, no protected attributes) plus the profile-embedding cosine similarity. The LLM never produces
or adjusts any number here; it only explains the result afterwards.

Every sub-score is 0-100 or ``None`` (no data). ``overall`` is the weighted mean of the non-null sub-scores,
rounded to one decimal.
"""

import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.schemas.matching_profiles import (
    CandidateMatchProfile,
    CandidateSkillFact,
    DemandMatchProfile,
    RequiredSkill,
    SkillGraph,
)

# "2" (Epic 37): graph expansion credits, proficiency factor, graph version in the input hash.
SCORING_VERSION = "2"

SUB_SCORES: tuple[str, ...] = (
    "skills",
    "experience",
    "location",
    "compensation",
    "availability",
    "domain",
    "similarity",
)
DEFAULT_WEIGHTS: dict[str, int] = {
    "skills": 35,
    "experience": 15,
    "location": 10,
    "compensation": 10,
    "availability": 10,
    "domain": 10,
    "similarity": 10,
}
WEIGHT_MIN, WEIGHT_MAX, SKILLS_WEIGHT_MIN = 0, 60, 15

CREDIT_EXACT, CREDIT_CHILD, CREDIT_PARENT = 1.0, 0.8, 0.4
UNKNOWN_YEARS_FACTOR = 0.5
MANDATORY_WEIGHT, NICE_WEIGHT = 2, 1
MISSING_CREDIT = 0.5

# Default FX table (units of INR per unit of currency); tenants override it in ``matching_settings``.
DEFAULT_FX: dict[str, Decimal] = {
    "INR": Decimal(1),
    "USD": Decimal("83.0"),
    "EUR": Decimal("90.0"),
    "GBP": Decimal("105.0"),
    "AED": Decimal("22.6"),
    "SGD": Decimal("61.5"),
    "AUD": Decimal("55.0"),
    "CAD": Decimal("61.0"),
}

_COUNTRY_ALIASES: dict[str, str] = {
    "in": "india",
    "ind": "india",
    "bharat": "india",
    "us": "united states",
    "usa": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "gb": "united kingdom",
    "great britain": "united kingdom",
    "england": "united kingdom",
    "ae": "united arab emirates",
    "uae": "united arab emirates",
    "sg": "singapore",
    "au": "australia",
    "ca": "canada",
    "de": "germany",
}


class WeightsError(ValueError):
    def __init__(self, fields: list[dict[str, str]]) -> None:
        self.fields = fields
        super().__init__("; ".join(f"{f['field']}: {f['message']}" for f in fields))


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenceItem:
    id: str
    kind: str
    text: str
    comp: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "text": self.text, "comp": self.comp}


@dataclasses.dataclass(frozen=True, slots=True)
class ScoreResult:
    overall: Decimal
    sub_scores: dict[str, int | None]
    evidence: tuple[EvidenceItem, ...]
    missing_mandatory: tuple[str, ...]
    credited_skills: int


# ================================================================ helpers
def norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def norm_country(text: str | None) -> str:
    key = norm(text).replace(".", "")
    return _COUNTRY_ALIASES.get(key, key)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> int:
    return round(max(low, min(high, value)))


def _fmt_years(value: Decimal | float) -> str:
    d = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = f"{d.normalize():f}"
    return text


def validate_weights(weights: Mapping[str, Any]) -> dict[str, int]:
    """Weights must name exactly the seven sub-scores, each 0-60, skills >= 15, sum > 0."""
    errors: list[dict[str, str]] = []
    unknown = sorted(set(weights) - set(SUB_SCORES))
    errors.extend({"field": f"weights.{k}", "message": "Unknown weight"} for k in unknown)
    out: dict[str, int] = {}
    for key in SUB_SCORES:
        raw = weights.get(key, DEFAULT_WEIGHTS[key])
        try:
            value = int(raw)
        except (TypeError, ValueError):
            errors.append({"field": f"weights.{key}", "message": "Must be a whole number"})
            continue
        if isinstance(raw, float) and not raw.is_integer():
            errors.append({"field": f"weights.{key}", "message": "Must be a whole number"})
            continue
        if not WEIGHT_MIN <= value <= WEIGHT_MAX:
            errors.append(
                {"field": f"weights.{key}", "message": f"Must be between {WEIGHT_MIN} and {WEIGHT_MAX}"}
            )
            continue
        out[key] = value
    if "skills" in out and out["skills"] < SKILLS_WEIGHT_MIN:
        errors.append({"field": "weights.skills", "message": f"Must be at least {SKILLS_WEIGHT_MIN}"})
    if not errors and sum(out.values()) <= 0:
        errors.append({"field": "weights", "message": "At least one weight must be above 0"})
    if errors:
        raise WeightsError(errors)
    return out


def convert(amount: Decimal, from_ccy: str, to_ccy: str, fx: Mapping[str, Decimal]) -> Decimal | None:
    src, dst = fx.get(from_ccy.upper()), fx.get(to_ccy.upper())
    if src is None or dst is None or dst == 0:
        return None
    return amount * src / dst


# ================================================================ sub-scores
def _expansion_map(demand: DemandMatchProfile) -> dict[Any, tuple[tuple[Any, Decimal, str], ...]]:
    return {e.required_skill_id: e.neighbours for e in demand.expansions}


def _skill_credit(
    req: RequiredSkill,
    by_id: Mapping[Any, CandidateSkillFact],
    by_name: Mapping[str, CandidateSkillFact],
    graph: SkillGraph,
    expansions: Mapping[Any, tuple[tuple[Any, Decimal, str], ...]] | None = None,
) -> tuple[float, CandidateSkillFact | None, str]:
    """(credit before years, the candidate skill that earned it, how).

    ``how`` is ``exact`` / ``child`` / ``parent`` (the P1 floor) or ``graph:<edge label>, <weight>``
    (Epic 37 expansion, computed before scoring). The P1 floor (descendant 0.8, single-hop parent 0.4) stays
    under the graph credit; ``expand=False`` on the Demand skill leaves only the exact match and the label
    fallback.
    """
    if req.skill_id is None:
        fact = by_name.get(norm(req.label))
        return (CREDIT_EXACT, fact, "exact") if fact is not None else (0.0, None, "none")
    if req.skill_id in by_id:
        return CREDIT_EXACT, by_id[req.skill_id], "exact"
    if req.expand:
        floor: tuple[float, CandidateSkillFact | None, str] = (0.0, None, "none")
        for child in sorted(graph.descendants_of(req.skill_id), key=str):
            if child in by_id:
                floor = (CREDIT_CHILD, by_id[child], "child")
                break
        if floor[1] is None:
            parent = graph.parent_of(req.skill_id)
            if parent is not None and parent in by_id:
                floor = (CREDIT_PARENT, by_id[parent], "parent")
        best: tuple[float, CandidateSkillFact | None, str] = (0.0, None, "none")
        neighbours = (expansions or {}).get(req.skill_id, ())
        for sid, weight, label in sorted(neighbours, key=lambda n: (-n[1], str(n[0]))):
            if sid in by_id and sid != req.skill_id:
                best = (float(weight), by_id[sid], f"graph:{label}, {weight.normalize():f}")
                break
        if best[0] > floor[0]:
            return best
        if floor[1] is not None:
            return floor
    fact = by_name.get(norm(req.label))  # label fallback (e.g. an unresolved tenant skill of the same name)
    return (CREDIT_EXACT, fact, "exact") if fact is not None else (0.0, None, "none")


def score_skills(
    demand: DemandMatchProfile, cand: CandidateMatchProfile, graph: SkillGraph
) -> tuple[int | None, list[EvidenceItem], list[str], int]:
    if not demand.skills:
        return None, [], [], 0
    by_id = {s.skill_id: s for s in cand.skills}
    by_name = {norm(s.name): s for s in cand.skills}
    expansions = _expansion_map(demand)
    total_w = earned = 0.0
    evidence: list[EvidenceItem] = []
    missing: list[str] = []
    mandatory_total = mandatory_met = credited = 0
    for req in demand.skills:
        weight = MANDATORY_WEIGHT if req.mandatory else NICE_WEIGHT
        credit, fact, how = _skill_credit(req, by_id, by_name, graph, expansions)
        if credit > 0 and req.min_years is not None and req.min_years > 0:
            if fact is None or fact.years is None:
                credit *= UNKNOWN_YEARS_FACTOR
            else:
                credit *= min(1.0, float(fact.years) / float(req.min_years))
        # Epic 37 (FR-190): proficiency factor; unknown proficiency is neutral (1.0) but made visible.
        unknown_proficiency = False
        if credit > 0 and req.min_proficiency is not None and fact is not None:
            if fact.proficiency is None:
                unknown_proficiency = True
            else:
                credit *= min(1.0, fact.proficiency / req.min_proficiency)
        total_w += weight
        earned += weight * credit
        if credit > 0:
            credited += 1
        if req.mandatory:
            mandatory_total += 1
            if credit < MISSING_CREDIT:
                missing.append(req.label)
                evidence.append(
                    EvidenceItem(
                        f"missing:{norm(req.label)}",
                        "missing_skill",
                        f"Mandatory skill '{req.label}' missing",
                    )
                )
                continue
            mandatory_met += 1
        if fact is not None and credit > 0:
            years = f"{_fmt_years(fact.years)} yrs " if fact.years is not None else ""
            if how.startswith("graph:"):
                via = f" ({how.removeprefix('graph:')})"
            else:
                via = "" if how == "exact" else f" (related to {req.label})"
            evidence.append(EvidenceItem(f"skill:{norm(req.label)}", "skill", f"{years}{fact.name}{via}"))
            if unknown_proficiency:
                evidence.append(
                    EvidenceItem(
                        f"proficiency:{norm(req.label)}",
                        "proficiency_unknown",
                        f"{fact.name} (proficiency not recorded)",
                    )
                )
    value = 100.0 * earned / total_w if total_w else 0.0
    if missing and mandatory_total:
        value = min(value, math.floor(60 * mandatory_met / mandatory_total))
    return _clamp(value), evidence, missing, credited


def score_experience(
    demand: DemandMatchProfile, cand: CandidateMatchProfile
) -> tuple[int | None, list[EvidenceItem]]:
    lo, hi, years = demand.experience_min, demand.experience_max, cand.experience_years
    if years is None or (lo is None and hi is None):
        return None, []
    y = float(years)
    if lo is not None and y < float(lo):
        value = _clamp(100 - 25 * (float(lo) - y))
    elif hi is not None and y > float(hi):
        value = _clamp(100 - 10 * (y - float(hi)), low=50)
    else:
        value = 100
    rng = f"{_fmt_years(lo) if lo is not None else '0'}-{_fmt_years(hi) if hi is not None else '+'}"
    return value, [
        EvidenceItem("experience", "experience", f"{_fmt_years(years)} yrs experience (needed {rng})")
    ]


def _work_auth_ok(demand: DemandMatchProfile, cand: CandidateMatchProfile) -> bool | None:
    if not demand.required_work_auth:
        return True
    have = [norm(w) for w in cand.work_auth if norm(w)]
    if not have:
        return None
    for need in (norm(w) for w in demand.required_work_auth):
        if any(need in h or h in need for h in have):
            return True
    return False


def score_location(
    demand: DemandMatchProfile, cand: CandidateMatchProfile
) -> tuple[int | None, list[EvidenceItem]]:
    auth = _work_auth_ok(demand, cand)
    if auth is False:
        return 0, [EvidenceItem("work_auth", "location", "Required work authorization not held")]
    if demand.remote:
        return 100, [EvidenceItem("location", "location", "Remote role")]
    d_city, d_state, d_country = norm(demand.city), norm(demand.state), norm_country(demand.country)
    if not (d_city or d_state or d_country):
        return None, []
    c_city, c_state, c_country = norm(cand.city), norm(cand.state), norm_country(cand.country)
    if not (c_city or c_state or c_country or cand.preferred_cities or cand.willing_to_relocate):
        return None, []
    where = cand.city or cand.state or cand.country or "unknown"
    if d_city and (d_city == c_city or d_city in {norm(p) for p in cand.preferred_cities}):
        return 100, [EvidenceItem("location", "location", f"Based in or prefers {demand.city}")]
    if cand.willing_to_relocate:
        return 80, [EvidenceItem("location", "location", f"Based in {where}; willing to relocate")]
    if d_state and d_state == c_state:
        return 60, [EvidenceItem("location", "location", f"Same state ({cand.state})")]
    if d_country and d_country == c_country:
        return 40, [EvidenceItem("location", "location", f"Same country ({cand.country})")]
    return 0, [EvidenceItem("location", "location", f"Based in {where}")]


def score_compensation(
    demand: DemandMatchProfile, cand: CandidateMatchProfile, fx: Mapping[str, Decimal]
) -> tuple[int | None, list[EvidenceItem]]:
    if (
        demand.budget_max_annual is None
        or cand.expected_comp_annual is None
        or not demand.currency
        or not cand.expected_comp_currency
        or demand.budget_max_annual <= 0
    ):
        return None, []
    expected = convert(cand.expected_comp_annual, cand.expected_comp_currency, demand.currency, fx)
    if expected is None:
        return None, []
    budget = demand.budget_max_annual
    if expected <= budget:
        value = 100
        text = f"Expected compensation within budget ({demand.currency})"
    else:
        pct_over = float((expected - budget) / budget * 100)
        value = _clamp(100 - 4 * pct_over)
        text = f"Expected compensation {round(pct_over)}% over budget"
    return value, [EvidenceItem("compensation", "compensation", text, comp=True)]


def score_availability(
    demand: DemandMatchProfile, cand: CandidateMatchProfile, today: date
) -> tuple[int | None, list[EvidenceItem]]:
    if demand.needed_by is None:
        return None, []
    if cand.available_from is not None:
        start = cand.available_from
        basis = f"Available from {start:%d %b}"
    elif cand.notice_days is not None:
        start = today + timedelta(days=cand.notice_days)
        basis = f"Notice {cand.notice_days} days"
    else:
        return None, []
    late = (start - demand.needed_by).days
    value = 100 if late <= 0 else _clamp(100 - 2 * late)
    return value, [
        EvidenceItem("availability", "availability", f"{basis}; needed by {demand.needed_by:%d %b}")
    ]


def score_domain(
    demand: DemandMatchProfile, cand: CandidateMatchProfile
) -> tuple[int | None, list[EvidenceItem]]:
    wanted = {norm(d) for d in demand.domains if norm(d)}
    if not wanted:
        return None, []
    have = {norm(d) for d in cand.domains if norm(d)}
    if not have:
        return None, []
    hit = wanted & have
    value = _clamp(100 * len(hit) / len(wanted))
    text = f"Domain experience: {', '.join(sorted(hit))}" if hit else "No matching domain experience"
    return value, [EvidenceItem("domain", "domain", text)]


def score_similarity(similarity: float | None) -> tuple[int | None, list[EvidenceItem]]:
    if similarity is None or math.isnan(similarity):
        return None, []
    value = _clamp(100 * max(0.0, similarity))
    return value, [EvidenceItem("similarity", "similarity", f"Profile similarity {value}")]


# ================================================================ overall
def overall_of(sub_scores: Mapping[str, int | None], weights: Mapping[str, int]) -> Decimal:
    total_w = sum(weights.get(k, 0) for k, v in sub_scores.items() if v is not None)
    if total_w <= 0:
        return Decimal("0.0")
    acc = sum(Decimal(weights.get(k, 0)) * Decimal(v) for k, v in sub_scores.items() if v is not None)
    return (acc / Decimal(total_w)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def score(
    demand: DemandMatchProfile,
    cand: CandidateMatchProfile,
    *,
    weights: Mapping[str, int],
    graph: SkillGraph,
    similarity: float | None,
    fx: Mapping[str, Decimal],
    today: date,
) -> ScoreResult:
    skills, skill_ev, missing, credited = score_skills(demand, cand, graph)
    experience, exp_ev = score_experience(demand, cand)
    location, loc_ev = score_location(demand, cand)
    compensation, comp_ev = score_compensation(demand, cand, fx)
    availability, av_ev = score_availability(demand, cand, today)
    domain, dom_ev = score_domain(demand, cand)
    sim, sim_ev = score_similarity(similarity)
    subs: dict[str, int | None] = {
        "skills": skills,
        "experience": experience,
        "location": location,
        "compensation": compensation,
        "availability": availability,
        "domain": domain,
        "similarity": sim,
    }
    evidence = (*skill_ev, *exp_ev, *loc_ev, *comp_ev, *av_ev, *dom_ev, *sim_ev)
    return ScoreResult(
        overall=overall_of(subs, weights),
        sub_scores=subs,
        evidence=evidence,
        missing_mandatory=tuple(missing),
        credited_skills=credited,
    )


# ================================================================ hashing
def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple | list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, Decimal | date):
        return str(value)
    if isinstance(value, float):
        return round(value, 6)
    return value if value is None or isinstance(value, bool | int | str) else str(value)


def input_hash(
    demand: DemandMatchProfile,
    cand: CandidateMatchProfile,
    *,
    weights: Mapping[str, int],
    similarity: float | None,
    fx: Mapping[str, Decimal],
    today: date,
    graph_version: str | None = None,
) -> str:
    """Hash of everything a score depends on (profiles + weights + FX + similarity + version + date + the
    skills graph version, Epic 37: a graph change makes matches stale rather than silently different)."""
    payload = {
        "v": SCORING_VERSION,
        "d": _jsonable(demand),
        "c": _jsonable(cand),
        "w": _jsonable(dict(weights)),
        "s": None if similarity is None else round(float(similarity), 6),
        "fx": _jsonable(dict(fx)),
        "t": str(today),
    }
    if graph_version is not None:
        payload["g"] = graph_version
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()

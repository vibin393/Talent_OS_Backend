# ruff: noqa: E501
"""Epic 37 pure units (no DB): expand traversal rules, derive() recompute rule, graph-aware scoring, seed file."""

import dataclasses
import itertools
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.core.errors import ValidationFailed
from app.schemas.matching_profiles import (
    CandidateMatchProfile,
    CandidateSkillFact,
    DemandMatchProfile,
    RequiredSkill,
    SkillExpansion,
    SkillGraph,
)
from app.services.candidates.common import normalize_skill, slugify
from app.services.candidates.skill_seed import SEED_FILE, load_seed
from app.services.matching import scoring
from app.services.skills import graph as g
from app.services.skills.expand import check_depth, to_expanded, traverse
from app.services.skills.recompute import Ev, derive
from app.services.skills.seed import GRAPH_SEED_FILE, load_edges, seed_version
from app.services.skills.settings import parse_weight

ALL = frozenset({"parent_of", "implies", "related_to"})
JAVA, SB, KOTLIN, JPA, MVC = (f"global:{uuid.UUID(int=i)}" for i in range(1, 6))


def _edge(src: str, dst: str, kind: str, w: str) -> g.Edge:
    return g.Edge(src, dst, kind, Decimal(w), "platform", None)


GRAPH = g.build(
    "v1",
    [
        _edge(SB, JAVA, "implies", "0.8"),
        _edge(KOTLIN, JAVA, "related_to", "0.5"),
        _edge(JAVA, JPA, "parent_of", "0.8"),
        _edge(SB, MVC, "parent_of", "0.8"),
    ],
)


def _rows(
    roots: list[str], depth: int = 2, kinds: frozenset[str] = ALL, mw: str = "0"
) -> list[tuple[str, str, int]]:
    return [
        (r.key, str(r.weight.quantize(Decimal("0.01"))), r.depth)
        for r in traverse(GRAPH, roots, depth, kinds, Decimal(mw))
    ]


# ================================================================ expand
def test_expand_matrix_example() -> None:
    assert _rows([JAVA]) == [
        (JAVA, "1.00", 0),
        # depth 1: -weight then key (JPA uuid 4 < SB uuid 2? key order applies only on ties)
        *sorted([(JPA, "0.80", 1), (SB, "0.80", 1)]),
        (KOTLIN, "0.50", 1),
        (MVC, "0.64", 2),
    ]


def test_implies_is_one_way() -> None:
    assert _rows([SB]) == [(SB, "1.00", 0), (MVC, "0.80", 1)]  # Java does not satisfy Spring Boot


def test_related_is_both_ways_and_kinds_filter() -> None:
    assert (JAVA, "0.50", 1) in _rows([KOTLIN], depth=1)
    assert _rows([JAVA], depth=1, kinds=frozenset({"parent_of"})) == [(JAVA, "1.00", 0), (JPA, "0.80", 1)]


def test_min_weight_drops_but_keeps_input() -> None:
    assert _rows([JAVA], mw="0.70") == [(JAVA, "1.00", 0), *sorted([(JPA, "0.80", 1), (SB, "0.80", 1)])]
    assert _rows([JAVA], mw="1") == [(JAVA, "1.00", 0)]


def test_depth_rules_and_order_by_input() -> None:
    assert _rows([JAVA], depth=0) == [(JAVA, "1.00", 0)]
    out = _rows([SB, JAVA], depth=1)
    assert out[0] == (SB, "1.00", 0) and out[2][0] == JAVA
    for bad in (3, -1, True):
        with pytest.raises(ValidationFailed) as exc:
            check_depth(bad)  # type: ignore[arg-type]
        assert exc.value.code == "depth_out_of_range"


def test_best_path_wins_and_via() -> None:
    a, b, c = (f"tenant:{uuid.UUID(int=i)}" for i in (10, 11, 12))
    graph = g.build(
        "v",
        [_edge(a, c, "related_to", "0.3"), _edge(a, b, "parent_of", "0.9"), _edge(b, c, "parent_of", "0.9")],
    )
    rows = {r.key: r for r in traverse(graph, [a], 2, ALL, Decimal(0))}
    assert rows[c].weight == Decimal("0.81") and rows[c].depth == 2 and rows[c].via == b
    exp = to_expanded(list(rows.values()))
    assert {e.key.as_str(): e.via.as_str() if e.via else None for e in exp}[c] == b


def test_parallel_edges_keep_highest_weight() -> None:
    graph = g.build(
        "v", [_edge(SB, JAVA, "implies", "0.6"), g.Edge(SB, JAVA, "implies", Decimal("0.9"), "tenant", None)]
    )
    assert [str(r.weight) for r in traverse(graph, [JAVA], 1, ALL, Decimal(0))][1] == "0.9"


def test_parse_weight() -> None:
    assert parse_weight("0.8") == Decimal("0.80") and parse_weight(1) == Decimal("1.00")
    for bad in ("1.2", 0, "abc", None, True, "0.333", -0.1):
        with pytest.raises(ValidationFailed) as exc:
            parse_weight(bad)
        assert exc.value.code == "skill_edge_weight_invalid"


# ================================================================ derive
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _ev(kind: str, p: int | None, minutes: int = 0, superseded: bool = False) -> Ev:
    return Ev(kind, p, T0 + timedelta(minutes=minutes), superseded)


def test_derive_rules() -> None:
    assert derive([]) is None
    assert derive([_ev("self", 5), _ev("recruiter", 3, 1)]) == (3, "verified")
    assert derive([_ev("recruiter", 3), _ev("self", 5, 1)]) == (3, "verified")  # verified wins over declared
    assert derive([_ev("recruiter", 3), _ev("assessment", 4, 1)]) == (4, "verified")  # max verified
    assert derive([_ev("self", 2), _ev("self", 4, 1), _ev("resume", 5, 2)]) == (4, "declared")
    assert derive([_ev("resume", 2), _ev("graph", None, 1), _ev("project", 3, 2)]) == (3, "inferred")
    assert derive([_ev("recruiter", 3, superseded=True), _ev("self", 2)]) == (2, "declared")
    assert derive([_ev("recruiter", None)]) == (None, "verified")


@pytest.mark.parametrize(
    "combo",
    list(itertools.product(["resume", "self", "recruiter", "assessment", "project", "graph"], repeat=2)),
)
def test_derive_exhaustive_pairs(combo: tuple[str, str]) -> None:
    evs = [_ev(combo[0], 2, 0), _ev(combo[1], 4, 1)]
    prof, ver = derive(evs)  # type: ignore[misc]
    classes = {"recruiter": "verified", "assessment": "verified", "self": "declared"}
    got = [classes.get(k, "inferred") for k in combo]
    expected = "verified" if "verified" in got else "declared" if "declared" in got else "inferred"
    assert ver == expected
    winners = [e for e, c in zip(evs, got, strict=True) if c == expected]
    assert prof == (
        max(e.proficiency for e in winners) if expected == "verified" else winners[-1].proficiency
    )  # type: ignore[type-var]
    # AI / graph / project alone never verify
    assert derive([_ev("graph", 5), _ev("project", 5), _ev("resume", 5)]) == (5, "inferred")


# ================================================================ scoring
TODAY = date(2026, 10, 1)
S_JAVA, S_SB, S_MVC, S_SPRING = (uuid.UUID(int=100 + i) for i in range(4))
P1_GRAPH = SkillGraph(parents={S_SB: S_SPRING, S_MVC: S_SPRING, S_SPRING: S_JAVA})


def _cand(*facts: CandidateSkillFact) -> CandidateMatchProfile:
    return CandidateMatchProfile(
        candidate_id=uuid.UUID(int=1),
        current_title=None,
        skills=facts,
        experience_years=None,
        city=None,
        state=None,
        country=None,
        preferred_cities=(),
        willing_to_relocate=None,
        work_auth=(),
        expected_comp_annual=None,
        expected_comp_currency=None,
        available_from=None,
        notice_days=None,
        domains=(),
        role_titles=(),
        employers=(),
        certifications=(),
    )


def _demand(req: RequiredSkill, expansions: tuple[SkillExpansion, ...] = ()) -> DemandMatchProfile:
    return DemandMatchProfile(
        demand_id=uuid.UUID(int=2),
        title="Java dev",
        skills=(req,),
        experience_min=None,
        experience_max=None,
        remote=True,
        city=None,
        state=None,
        country=None,
        required_work_auth=(),
        budget_max_annual=None,
        currency=None,
        needed_by=None,
        domains=(),
        expansions=expansions,
    )


def _score(d: DemandMatchProfile, c: CandidateMatchProfile) -> tuple[int | None, list[Any]]:
    value, ev, _, _ = scoring.score_skills(d, c, P1_GRAPH)
    return value, [e.text for e in ev]


SB_REQ = RequiredSkill(S_SB, "Spring Boot", False, None, min_proficiency=3)
SB_EXP = (
    SkillExpansion(
        S_SB, ((S_MVC, Decimal("0.64"), "via Spring Boot"), (S_JAVA, Decimal("0.80"), "implies X"))
    ),
)


def test_expansion_credit_and_p1_floor() -> None:
    # no expansions -> exactly P1: Spring MVC is not a descendant of Spring Boot, Java is no parent of SB (single hop)
    assert _score(_demand(SB_REQ), _cand(CandidateSkillFact(S_MVC, "Spring MVC", proficiency=3)))[0] == 0
    value, ev = _score(_demand(SB_REQ, SB_EXP), _cand(CandidateSkillFact(S_MVC, "Spring MVC", proficiency=3)))
    assert value == 64 and ev == ["Spring MVC (via Spring Boot, 0.64)"]
    # P1 parent floor (0.4) kept for a direct parent
    spring_req = dataclasses.replace(SB_REQ, skill_id=S_SB)
    assert _score(_demand(spring_req), _cand(CandidateSkillFact(S_SPRING, "Spring", proficiency=5)))[0] == 40


def test_proficiency_factor_neutral_and_expand_off() -> None:
    value, ev = _score(_demand(SB_REQ), _cand(CandidateSkillFact(S_SB, "Spring Boot", Decimal(4))))
    assert value == 100 and "Spring Boot (proficiency not recorded)" in ev
    assert _score(_demand(SB_REQ), _cand(CandidateSkillFact(S_SB, "Spring Boot", proficiency=2)))[0] == 67
    off = dataclasses.replace(SB_REQ, expand=False)
    assert _score(_demand(off, SB_EXP), _cand(CandidateSkillFact(S_MVC, "Spring MVC", proficiency=3)))[0] == 0


def test_input_hash_determinism_and_graph_version() -> None:
    d, c = _demand(SB_REQ, SB_EXP), _cand(CandidateSkillFact(S_MVC, "Spring MVC", proficiency=3))
    kw: dict[str, Any] = {
        "weights": scoring.DEFAULT_WEIGHTS,
        "similarity": None,
        "fx": scoring.DEFAULT_FX,
        "today": TODAY,
    }
    h1 = scoring.input_hash(d, c, graph_version="a", **kw)
    assert h1 == scoring.input_hash(d, c, graph_version="a", **kw)
    assert h1 != scoring.input_hash(d, c, graph_version="b", **kw)
    r1 = scoring.score(d, c, graph=P1_GRAPH, **kw)
    assert r1 == scoring.score(d, c, graph=P1_GRAPH, **kw)
    assert scoring.SCORING_VERSION == "2"


# ================================================================ seed file
def test_seed_integrity() -> None:
    seed = load_seed(GRAPH_SEED_FILE)
    assert len(seed) >= 1500
    names = {s.name for s in seed}
    assert len({s.slug for s in seed}) == len(seed)
    p1 = load_seed(SEED_FILE)
    graph_by_name = {s.name: s for s in seed}
    for s in p1:  # superset, same slug / parent
        assert s.name in names and graph_by_name[s.name].slug == s.slug == slugify(s.name)
        assert graph_by_name[s.name].parent == s.parent
    aliases = [a for s in seed for a in s.aliases]
    assert len(aliases) == len(set(aliases))
    assert not set(aliases) & {normalize_skill(n) for n in names}
    parents = {s.name: s.parent for s in seed}
    for s in seed:
        assert s.parent is None or s.parent in names
        seen, cur = set(), s.name
        while cur is not None:
            assert cur not in seen, f"parent cycle at {cur}"
            seen.add(cur)
            cur = parents.get(cur)
    edges = load_edges(GRAPH_SEED_FILE)
    assert edges and seed_version(GRAPH_SEED_FILE) != "0"
    keys = set()
    for e in edges:
        assert e.from_name in names and e.to_name in names and e.from_name != e.to_name
        assert Decimal(0) < e.weight <= Decimal(1)
        assert (e.from_name, e.to_name, e.kind) not in keys
        keys.add((e.from_name, e.to_name, e.kind))
    assert ("Spring Boot", "Java", "implies") in keys

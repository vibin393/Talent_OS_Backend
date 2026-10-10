"""Epic 36 (spec 0.1 / A1): CI guards on Alembic revision ids, Phase 3 branch shape and DB identifier lengths.

(a) every revision id <= 32 chars; (b) Phase 3 revisions (descendants of the Phase 2 merge) are named
``<yyyymmdd>_<band 300..316>_<nn>_<slug <= 16>`` or ``<yyyymmdd>_900_merge_phase3``; (c) the first revision of
each ``p3_*`` epic branch forks from the foundation head and carries a build-plan label; (d) the only cross-branch
``depends_on`` is campaigns (303) -> ``<date>_302_01_segments``; (e) every constraint / index / trigger / function
name in ``Base.metadata`` and in Phase 3 migration sources is <= 63 chars.
"""

import itertools
import re
from pathlib import Path

import pytest

import app.models  # noqa: F401 - registers every table
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.models import Base

BACKEND = Path(__file__).resolve().parents[2]
PHASE2_MERGE = "20261006_900_merge_phase2"
FOUNDATION = ("20261007_300_01_p3_checks", "20261007_300_02_p3_hubs", "20261007_300_03_p3_alters")
FOUNDATION_HEAD = FOUNDATION[-1]
P3_ID = re.compile(r"^\d{8}_(3(0\d|1[0-6])_\d{2}_[a-z0-9_]{1,16}|900_merge_phase3)$")
P3_LABELS = {
    "p3_skills",
    "p3_pools",
    "p3_campaigns",
    "p3_assessments",
    "p3_mobility",
    "p3_referrals",
    "p3_campus",
    "p3_workforce",
    "p3_projects",
    "p3_immigration",
    "p3_reports",
    "p3_analytics",
    "p3_fairness",
    "p3_client_sso",
    "p3_fin_carry",
    "p3_governance",
}
# Phase 1 / Phase 2 metadata names generated over 63 chars (PostgreSQL truncated them on disk); grandfathered,
# shrink-only. No new name may join this list.
LEGACY_LONG_NAMES = frozenset(
    {
        "uq_interview_invite_log_interview_id_sequence_participant_id_method",
        "uq_billing_schedule_items_tenant_id_placement_id_kind_period_start",
        "uq_crm_record_links_tenant_id_opportunity_id_record_kind_record_id",
        "ck_onboarding_template_tasks_ck_onboarding_template_tasks_assignee",
        "ck_onboarding_template_tasks_ck_onboarding_template_tasks_owner_kind",
    }
)
NAME = re.compile(r"""["']((?:ck|fk|uq|ix|pk|tos)_[A-Za-z0-9_]+)["']""")


def _script() -> ScriptDirectory:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _phase3(script: ScriptDirectory) -> list:
    """Revisions from the Phase 2 merge up to the Phase 3 close (the Phase 3 tree; Phase 4 excluded)."""
    p4 = {r.revision for r in _phase4(script)}
    p4 |= {r.revision for r in script.walk_revisions(base="20261009_900_merge_phase4", head="heads")}  # + Phase 5
    return [
        r
        for r in script.walk_revisions(base=PHASE2_MERGE, head="heads")
        if r.revision != PHASE2_MERGE and r.revision not in p4
    ]


# ---------------------------------------------------------------- Phase 4 (Epic 53, build plan 1)
PHASE3_MERGE = "20261007_900_merge_phase3"
P4_FOUNDATION = (
    "20261008_400_01_p4_checks",
    "20261008_400_02_p4_runtime",
    "20261008_400_03_p4_hubs",
    "20261008_400_04_p4_alters",
)
P4_FOUNDATION_HEAD = P4_FOUNDATION[-1]
P4_ID = re.compile(r"^\d{8}_(4(0\d|1[0-2])_\d{2}_[a-z0-9_]{1,16}|900_merge_phase4)$")


def _phase4(script: ScriptDirectory) -> list:
    """Revisions descending from the Phase 3 merge (the Phase 4 tree)."""
    try:
        p5 = {r.revision for r in script.walk_revisions(base="20261009_900_merge_phase4", head="heads")}
        p5.discard("20261009_900_merge_phase4")
        return [
            r
            for r in script.walk_revisions(base=PHASE3_MERGE, head="heads")
            if r.revision != PHASE3_MERGE and r.revision not in p5  # Epic 66: Phase 5 descends from the P4 merge
        ]
    except Exception:  # pragma: no cover - before Phase 4 existed
        return []


def test_phase4_ids_and_foundation_chain() -> None:
    script = _script()
    p4 = _phase4(script)
    ids = {r.revision for r in p4}
    assert set(P4_FOUNDATION) <= ids
    bad = sorted(r for r in ids if not P4_ID.match(r))
    assert bad == [], f"Phase 4 revision ids must match {P4_ID.pattern}: {bad}"
    first = script.get_revision(P4_FOUNDATION[0])
    assert first.down_revision == PHASE3_MERGE and set(first._orig_branch_labels or ()) == {"p4_foundation"}
    for prev, rev in itertools.pairwise(P4_FOUNDATION):
        assert script.get_revision(rev).down_revision == prev


def test_phase4_branches_fork_from_foundation_head_without_depends_on() -> None:
    script = _script()
    for rev in _phase4(script):
        deps = rev.dependencies
        deps = (deps,) if isinstance(deps, str) else tuple(deps or ())
        if rev.revision.endswith("_900_merge_phase4"):
            continue
        assert deps == (), f"{rev.revision}: Phase 4 allows no cross-branch depends_on"
        labels = set(rev._orig_branch_labels or ())
        if rev.revision in P4_FOUNDATION or not labels:
            continue
        assert rev.down_revision == P4_FOUNDATION_HEAD, f"{rev.revision}: must fork from {P4_FOUNDATION_HEAD}"
        assert all(label.startswith("p4_") for label in labels), labels
        assert 401 <= int(rev.revision.split("_")[1]) <= 412


def test_every_revision_id_fits() -> None:
    long = [r.revision for r in _script().walk_revisions() if len(r.revision) > 32]
    assert long == []


def test_phase3_ids_and_foundation_chain() -> None:
    script = _script()
    p3 = _phase3(script)
    ids = {r.revision for r in p3}
    assert set(FOUNDATION) <= ids
    bad = sorted(r for r in ids if not P3_ID.match(r))
    assert bad == [], f"Phase 3 revision ids must match {P3_ID.pattern}: {bad}"
    first = script.get_revision(FOUNDATION[0])
    assert first.down_revision == PHASE2_MERGE and set(first._orig_branch_labels or ()) == {"p3_foundation"}
    assert script.get_revision(FOUNDATION[1]).down_revision == FOUNDATION[0]
    assert script.get_revision(FOUNDATION[2]).down_revision == FOUNDATION[1]


def test_epic_branches_fork_from_foundation_head() -> None:
    script = _script()
    for rev in _phase3(script):
        labels = set(rev._orig_branch_labels or ())
        if rev.revision in FOUNDATION or not labels:
            continue
        assert rev.down_revision == FOUNDATION_HEAD, (
            f"{rev.revision}: first branch revision must fork from foundation"
        )
        assert labels <= P3_LABELS, f"{rev.revision}: unknown branch label(s) {labels - P3_LABELS}"
        band = int(rev.revision.split("_")[1])
        assert 301 <= band <= 316


def test_only_campaigns_depend_on_segments() -> None:
    script = _script()
    p3 = _phase3(script)
    p3_ids = {r.revision for r in p3}
    for rev in p3:
        deps = rev.dependencies
        deps = (deps,) if isinstance(deps, str) else tuple(deps or ())
        if not deps:
            continue
        if rev.revision.endswith("_900_merge_phase3"):
            # The Phase 3 close repeats the Phase 2 merge's ``depends_on`` (two effective Phase 2 heads). It must
            # never name a Phase 3 revision, and each target must already be an ancestor of the Phase 2 merge.
            assert not set(deps) & p3_ids, deps
            p2_ancestors = {r.revision for r in script.iterate_revisions(PHASE2_MERGE, "base")}
            assert set(deps) <= p2_ancestors, deps
            continue
        assert rev.revision.split("_")[1] == "303", f"{rev.revision}: cross-branch depends_on not allowed"
        assert all(re.match(r"^\d{8}_302_01_segments$", d) for d in deps), deps


def test_db_identifiers_fit_63_chars() -> None:
    too_long: list[str] = []
    for table in Base.metadata.tables.values():
        for c in table.constraints:
            if c.name is not None and len(str(c.name)) > 63:
                too_long.append(str(c.name))
        for ix in table.indexes:
            if ix.name is not None and len(str(ix.name)) > 63:
                too_long.append(str(ix.name))
    p3_files = [
        p for p in (BACKEND / "alembic" / "versions").glob("*.py") if p.name >= "20261007_3"
    ]  # incl. Phase 4
    p3_files += list((BACKEND / "alembic" / "versions").glob("*_900_merge_phase3.py"))
    for path in p3_files:
        too_long += [m for m in NAME.findall(path.read_text(encoding="utf-8")) if len(m) > 63]
    assert sorted(set(too_long) - LEGACY_LONG_NAMES) == []


# Epic 65 (build plan §7.4): after the Phase 4 close CI fails again on more than one head.
def test_single_head_after_phase4_close() -> None:
    script = _script()
    merges = [r for r in script.walk_revisions() if r.revision.endswith("_900_merge_phase4")]
    if not merges:
        pytest.skip("phase-4 merge not created yet")
    effective = set(merges[0].dependencies or ())  # the P2/P3 merges' display artefact, merged via depends_on
    heads = set(script.get_heads()) - effective
    # Epic 66: once Phase 5 starts, every real head descends from the Phase 4 merge (one per p5_* branch until the
    # Phase 5 close); before that the merge itself is the only head.
    p5 = {r.revision for r in script.walk_revisions(base=merges[0].revision, head="heads")} - {merges[0].revision}
    assert heads == {merges[0].revision} or (heads and heads <= p5)
    assert effective == {"20261003_201_03_external_auth", "20261005_216_04_billing_notes"}


# ---------------------------------------------------------------- Phase 5 (Epic 66, build plan §1)
PHASE4_MERGE = "20261009_900_merge_phase4"
P5_FOUNDATION = (
    "20261009_500_01_p5_checks",
    "20261009_500_02_p5_oauth",
    "20261009_500_03_p5_platform",
    "20261009_500_04_p5_alters",
    "20261009_500_05_p5_vault_move",
    "20261009_500_06_p5_vault_drop",
)
P5_FOUNDATION_HEAD = P5_FOUNDATION[-1]
P5_ID = re.compile(r"^\d{8}_(5(0\d|1[0-3])_\d{2}_[a-z0-9_]{1,16}|900_merge_phase5)$")
P5_LABELS = {
    "p5_public_api", "p5_webhooks", "p5_marketplace", "p5_job_boards", "p5_hrms_ledger", "p5_calendar_sync", "p5_mcp",
    "p5_dev_portal", "p5_job_network", "p5_telephony", "p5_portal_copilot", "p5_p4_hardening", "p5_governance",
}  # fmt: skip


def _phase5(script: ScriptDirectory) -> list:
    return [r for r in script.walk_revisions(base=PHASE4_MERGE, head="heads") if r.revision != PHASE4_MERGE]


def test_phase5_ids_and_foundation_chain() -> None:
    script = _script()
    p5 = _phase5(script)
    ids = {r.revision for r in p5}
    assert set(P5_FOUNDATION) <= ids
    bad = sorted(r for r in ids if not P5_ID.match(r))
    assert bad == [], f"Phase 5 revision ids must match {P5_ID.pattern}: {bad}"
    first = script.get_revision(P5_FOUNDATION[0])
    assert first.down_revision == PHASE4_MERGE and set(first._orig_branch_labels or ()) == {"p5_foundation"}
    for prev, rev in itertools.pairwise(P5_FOUNDATION):
        assert script.get_revision(rev).down_revision == prev


def test_phase5_branches_fork_from_foundation_head_without_depends_on() -> None:
    script = _script()
    for rev in _phase5(script):
        if rev.revision.endswith("_900_merge_phase5"):
            continue
        deps = rev.dependencies
        deps = (deps,) if isinstance(deps, str) else tuple(deps or ())
        assert deps == (), f"{rev.revision}: Phase 5 allows no cross-branch depends_on"
        labels = set(rev._orig_branch_labels or ())
        if rev.revision in P5_FOUNDATION or not labels:
            continue
        assert rev.down_revision == P5_FOUNDATION_HEAD, f"{rev.revision}: must fork from {P5_FOUNDATION_HEAD}"
        assert labels <= P5_LABELS, labels
        assert 501 <= int(rev.revision.split("_")[1]) <= 513

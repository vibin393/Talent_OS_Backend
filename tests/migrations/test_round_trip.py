"""Story 22.9 (A10): migration round trip on a scratch database (``-m slow``).

``upgrade heads`` -> downgrade every Phase 2 branch back to the foundation head (``20261002_200_07``),
one branch at a time to its base revision (branch-local), then the bases to the foundation head ->
``upgrade heads``. Branch labels are inherited by descendants, so a branch is identified by the label its
base revision declares.
"""

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.settings import get_settings

pytestmark = pytest.mark.slow

BACKEND_DIR = Path(__file__).resolve().parents[2]
FOUNDATION_HEAD = "20261002_200_07"
# Unique per run: concurrent runs must never drop each other's scratch database.
SCRATCH_DB = os.environ.get("ROUND_TRIP_DB_NAME") or f"talent_os_main_test_e35_rt_{os.getpid()}"
FORBIDDEN = {"talent_os_test", "talent_os_main"}


def _config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["db_url"] = get_settings().owner_db_url(SCRATCH_DB)
    cfg.attributes["configure_logger"] = False
    return cfg


async def _admin(sql: str) -> None:
    s = get_settings()
    conn = await asyncpg.connect(
        user=s.db_user, password=s.db_password, host=s.db_host, port=s.db_port, database="postgres"
    )
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _heads() -> set[str]:
    s = get_settings()
    conn = await asyncpg.connect(
        user=s.db_user, password=s.db_password, host=s.db_host, port=s.db_port, database=SCRATCH_DB
    )
    try:
        return {r["version_num"] for r in await conn.fetch("SELECT version_num FROM alembic_version")}
    finally:
        await conn.close()


def phase2_branches(script: ScriptDirectory) -> dict[str, str]:
    """``{label: base revision}`` for every branch rooted on the foundation head."""
    out: dict[str, str] = {}
    for rev in script.walk_revisions():
        declared = set(rev._orig_branch_labels or ())
        if rev.down_revision == FOUNDATION_HEAD and declared:
            for label in declared:
                out[label] = rev.revision
    return out


def test_phase2_branches_downgrade_and_upgrade_again() -> None:
    assert SCRATCH_DB not in FORBIDDEN and get_settings().db_name != SCRATCH_DB
    cfg = _config()
    script = ScriptDirectory.from_config(cfg)
    branches = phase2_branches(script)
    assert branches, "no Phase 2 branches found"
    # ``alembic_version`` omits heads that another branch ``depends_on`` (they are implied by the dependent).
    implied = {
        d
        for r in script.walk_revisions()
        for d in (r.dependencies or ())
        if isinstance(r.dependencies, tuple)
    }
    implied |= {r.dependencies for r in script.walk_revisions() if isinstance(r.dependencies, str)}
    expected_heads = set(script.get_heads()) - implied
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
    asyncio.run(_admin(f'CREATE DATABASE "{SCRATCH_DB}"'))
    failures: dict[str, str] = {}
    try:
        command.upgrade(cfg, "heads")
        assert asyncio.run(_heads()) == expected_heads
        pending = sorted(branches)
        # Branches that others depend on (``depends_on`` / merge parents) can only go once their dependents
        # are gone: retry deferred branches until no progress is made.
        while pending:
            progressed, deferred = False, []
            for label in pending:
                members = {r.revision for r in script.walk_revisions() if label in r.branch_labels}
                if not asyncio.run(_heads()) & members:  # removed already with a branch it depends on
                    progressed = True
                    continue
                try:
                    # Branch-local: only heads descending from the branch base move (``label@-n`` is
                    # ambiguous because labels are inherited).
                    command.downgrade(cfg, branches[label])
                    progressed = True
                except Exception as exc:
                    deferred.append(label)
                    failures[label] = f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0][:300]}"
            if not progressed:
                break
            for label in set(pending) - set(deferred):
                failures.pop(label, None)
            pending = deferred
        assert not failures, f"branch downgrades failed: {failures}"
        # Every branch now sits at its base revision; the bases go together (each downgrade() still runs).
        command.downgrade(cfg, FOUNDATION_HEAD)
        assert asyncio.run(_heads()) == {FOUNDATION_HEAD}
        command.upgrade(cfg, "heads")
        assert asyncio.run(_heads()) == expected_heads
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))


def test_phase3_foundation_downgrade_and_upgrade_again() -> None:
    """Epic 36: ``upgrade heads`` -> ``downgrade 20261006_900_merge_phase2`` -> ``upgrade heads`` (A0.3).

    Since the Phase 3 close the single head is ``20261007_900_merge_phase3``; downgrading to the Phase 2 merge
    still unwinds the merge, every Phase 3 epic branch and the foundation, and ``upgrade heads`` replays them.
    """
    assert SCRATCH_DB not in FORBIDDEN and get_settings().db_name != SCRATCH_DB
    cfg = _config()
    script = ScriptDirectory.from_config(cfg)
    merge = script.get_revision("20261007_900_merge_phase3")
    # ``get_heads()`` also lists the merge's ``depends_on`` targets (effective heads, implied in ``alembic_version``).
    assert set(script.get_heads()) - set(merge.dependencies or ()) == {merge.revision}
    merge_ancestors = {r.revision for r in script.iterate_revisions("20261007_900_merge_phase3", "base")}
    assert "20261007_300_03_p3_alters" in merge_ancestors
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
    asyncio.run(_admin(f'CREATE DATABASE "{SCRATCH_DB}"'))
    try:
        command.upgrade(cfg, "heads")
        command.downgrade(cfg, "20261006_900_merge_phase2")
        assert asyncio.run(_heads()) == {"20261006_900_merge_phase2"}
        command.upgrade(cfg, "heads")
        assert asyncio.run(_heads()) == {"20261007_900_merge_phase3"}
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))

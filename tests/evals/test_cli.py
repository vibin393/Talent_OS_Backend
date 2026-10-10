# ruff: noqa: E501
"""Epic 65 story 65.5 (FR-322, NFR-45): the evals CLI -- offline gate exit codes, the live ``--dry-run`` skip, the
EvalReport schema, the fingerprint, the live-database safety checks, the store and the release gate."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from evals import gate, live, publish, run, store
from evals.publish import CaseResult

AGENT = "screening"


def _results(fail_redteam: bool = False, agent_fail: int = 0) -> list[CaseResult]:
    out = [
        CaseResult(
            "screening",
            f"s{i}",
            AGENT,
            passed=i >= agent_fail,
            problems=() if i >= agent_fail else ("status failed",),
        )
        for i in range(10)
    ]
    out += [
        CaseResult(
            "redteam",
            f"r{i}",
            AGENT,
            passed=not (fail_redteam and i == 0),
            problems=("tool x executed",) if fail_redteam and i == 0 else (),
        )
        for i in range(5)
    ]
    return out


def test_offline_gate_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run, "execute", lambda *a, **k: _results())
    out = tmp_path / "report.json"
    assert run.main(["--mode", "offline", "--gate", "--json", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert set(report) == {"run", "results"}
    assert {"mode", "trigger", "git_sha", "models", "started_at", "finished_at", "status"} <= set(
        report["run"]
    )
    row = next(r for r in report["results"] if r["suite"] == "agent")
    assert {
        "agent_key",
        "agent_version",
        "suite",
        "prompt_ref",
        "model",
        "fingerprint",
        "cases_total",
        "cases_passed",
        "score",
        "threshold",
        "passed",
        "failures",
    } <= set(row)
    assert row["score"] == "1.0000" and row["threshold"] == "0.90"

    monkeypatch.setattr(run, "execute", lambda *a, **k: _results(fail_redteam=True))
    assert run.main(["--mode", "offline", "--gate"]) == 1
    assert "r0" in capsys.readouterr().out
    monkeypatch.setattr(run, "execute", lambda *a, **k: _results(agent_fail=2))  # 0.8 < 0.9
    assert run.main(["--mode", "offline", "--gate", "--json", str(out)]) == 1
    failures = json.loads(out.read_text(encoding="utf-8"))["results"][0]["failures"]
    assert failures and set(failures[0]) == {"case_id", "assertion", "code"}
    assert "failed" not in failures[0]["assertion"] or failures[0]["assertion"] == "status"
    monkeypatch.setattr(run, "execute", lambda *a, **k: [])
    assert run.main(["--mode", "offline", "--gate"]) == 1  # nothing ran


def test_gate_not_enforced_is_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core.settings import get_settings

    monkeypatch.setattr(get_settings(), "eval_gate_enforced", False)
    monkeypatch.setattr(run, "execute", lambda *a, **k: _results(fail_redteam=True))
    out = tmp_path / "r.json"
    assert run.main(["--gate", "--json", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["run"]["models"]["_gate_enforced"] == "false"


def test_live_dry_run_and_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY_LIVE", raising=False)
    assert run.main(["--mode", "live", "--dry-run"]) == 0
    assert "skipped: no live key" in capsys.readouterr().out
    assert run.main(["--mode", "live"]) == 2


def test_live_target_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.settings import get_settings

    s = get_settings()
    with pytest.raises(live.UnsafeTarget):
        live.check_target("postgresql+asyncpg://u:p@localhost/talent_os_test")
    with pytest.raises(live.UnsafeTarget):
        live.check_target(s.app_db_url())
    with pytest.raises(live.UnsafeTarget):
        live.check_target("")
    assert (
        live.check_target("postgresql+asyncpg://u:p@localhost/talent_os_main_evals") == "talent_os_main_evals"
    )
    monkeypatch.setattr(s, "app_env", "production")
    with pytest.raises(live.UnsafeTarget):
        live.check_target("postgresql+asyncpg://u:p@localhost/scratch")
    monkeypatch.setenv("ANTHROPIC_API_KEY_LIVE", "sk-test")
    monkeypatch.setattr(s, "eval_database_url", "postgresql+asyncpg://u:p@localhost/talent_os_test")
    monkeypatch.setattr(run, "execute", lambda *a, **k: pytest.fail("must refuse before running"))
    assert run.main(["--mode", "live"]) == 2


def test_fingerprint_changes_with_prompt(tmp_path: Path) -> None:
    gate.eval_agents()
    base = gate.fingerprint(AGENT)
    assert base == gate.fingerprint(AGENT)
    extra = tmp_path / "x.md"
    extra.write_text("one", encoding="utf-8")
    a = gate.fingerprint(AGENT, extra_files=[extra])
    extra.write_text("two", encoding="utf-8")
    assert a != gate.fingerprint(AGENT, extra_files=[extra]) and a != base
    assert gate.fingerprint(AGENT, models={"standard": "other-model"}) != base


def test_release_reasons() -> None:
    now = datetime.now(UTC)
    fp = "f" * 64
    ok = [
        {"suite": "agent", "fingerprint": fp, "passed": True, "created_at": now},
        {"suite": "redteam", "fingerprint": fp, "passed": True, "created_at": now},
    ]
    assert gate.release_reasons(AGENT, fp, ok, now) == []
    assert gate.release_reasons(AGENT, "other", ok, now) == ["missing_live_result"]
    stale = [{**r, "created_at": now - timedelta(days=8)} for r in ok]
    assert gate.release_reasons(AGENT, fp, stale, now) == ["stale_result"]
    low = [{**ok[0], "passed": False}, ok[1]]
    assert gate.release_reasons(AGENT, fp, low, now) == ["below_threshold"]
    red = [ok[0], {**ok[1], "passed": False}]
    assert gate.release_reasons(AGENT, fp, red, now) == ["redteam_failed"]


def test_release_gate_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY_LIVE", raising=False)
    assert gate.main(["--release", "--dry-run", "--allow-skip"]) == 0
    assert gate.main(["--release", "--dry-run"]) == 1


async def test_store_records_no_case_content() -> None:
    from app.models.ai_evals import AIEvalResult, AIEvalRun

    now = datetime.now(UTC)
    report = publish.build_report(
        _results(agent_fail=1), mode="live", trigger="manual", started_at=now, finished_at=now
    )
    run_id = await store.record(report)
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(store.results_url())
    try:
        async with eng.connect() as conn:
            r = (await conn.execute(select(AIEvalRun).where(AIEvalRun.id == run_id))).one()
            rows = (await conn.execute(select(AIEvalResult).where(AIEvalResult.run_id == run_id))).all()
    finally:
        await eng.dispose()
    assert r.mode == "live" and len(rows) == 2
    agent_row = next(x for x in rows if x.suite == "agent")
    assert agent_row.failures == [{"case_id": "s0", "assertion": "status", "code": "failed"}]
    live_rows = await store.live_results([AGENT])
    assert any(x["fingerprint"] == agent_row.fingerprint for x in live_rows[AGENT])
    assert isinstance(run_id, uuid.UUID)

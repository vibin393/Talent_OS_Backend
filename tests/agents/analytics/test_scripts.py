# ruff: noqa: E501
"""Epic 64 Fake-LLM scripts (``evals/analytics/scripts/*.yaml``): catalogue / run / masking / repair / clarify / injection /
SQL attempt, run end to end through the agent runtime (queue ``conversations``, origin ``copilot``)."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import update

from app.core.agentic import get_agent
from app.core.database import tenant_session
from app.models.clients import Client
from tests.agents.analytics.helpers import enable, run_agent, run_state
from tests.reporting.builder.helpers import seed_placements
from tests.reporting.helpers import ROrg

SCRIPTS = Path(__file__).resolve().parents[3] / "evals" / "analytics" / "scripts"
CASES = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(SCRIPTS.glob("*.yaml"))]


def test_script_corpus() -> None:
    ids = {c["id"] for c in CASES}
    assert {
        "placements_by_client_fy", "masked_bill", "forbidden_filter", "unknown_field_repair", "ambiguous_quarter",
        "clarify", "injection_cell", "sql_attempt", "copilot_capability",
    } <= ids  # fmt: skip


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_script(rorg: ROrg, case: dict[str, Any]) -> None:
    agent_key = "analytics"
    if case.get("capability"):
        if get_agent("copilot").placeholder:
            pytest.skip("the Copilot agent (Epic 54) is not registered yet; covered by test_capability.py")
        agent_key = "copilot"
    seeded = await seed_placements(rorg)
    rename = (case.get("setup") or {}).get("rename_client")
    if rename:
        async with tenant_session(rorg.tenant_id) as db:
            await db.execute(update(Client).where(Client.id == seeded.globex).values(name=rename))
    await enable(rorg.tenant_id, agent_copilot=True)
    member = {"admin": rorg.admin, "recruiter": rorg.recruiter}[case["persona"]]
    run_id = await run_agent(rorg.tenant_id, member.user_id, case["script"], agent_key=agent_key)
    run, calls, answers = await run_state(rorg.tenant_id, run_id)
    expect = case["expect"]
    assert run.status == expect["status"], (run.status, run.reason_code)
    seq = [[c.tool_key, c.outcome, *([c.error_code] if c.error_code else [])] for c in calls]
    want = [list(s) for s in expect["sequence"]]
    assert [s[: len(w)] for s, w in zip(seq, want, strict=False)] == want and len(seq) == len(want), seq
    assert len(answers) == expect["answers"]
    for key, value in (expect.get("answer") or {}).items():
        got = getattr(answers[0], key)
        assert (sorted(got) if isinstance(value, list) and key == "masked_fields" else got) == value, (
            key,
            got,
        )
    if expect.get("target_on"):
        call = next(c for c in calls if c.tool_key == expect["target_on"] and c.outcome == "executed")
        assert (call.target_type, call.target_id) == ("analytics_answer", answers[0].id)
    if "tainted" in expect:
        assert run.tainted is expect["tainted"]
    assert all(c.risk == "read" for c in calls) and run.write_count == 0


async def test_tools_refused_without_report_read(rorg: ROrg) -> None:
    from tests.reporting.builder.helpers import revoke

    await enable(rorg.tenant_id)
    await revoke(rorg, "recruiter", "report:read")
    script = [
        {"kind": "tool_calls", "calls": [["semantic.catalogue", {}]]},
        {"kind": "text", "text": "No access."},
    ]
    run_id = await run_agent(rorg.tenant_id, rorg.recruiter.user_id, script)
    _, calls, answers = await run_state(rorg.tenant_id, run_id)
    assert [(c.tool_key, c.outcome) for c in calls] == [("semantic.catalogue", "refused")] and answers == []

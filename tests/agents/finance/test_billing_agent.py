# ruff: noqa: E501
"""Epic 63 Billing Agent with the Fake LLM: preview -> unapproved time -> rates -> exception note -> run Proposal
(``invoice_run_prepared``); approval by a Finance User runs P2 ``create_run`` as that user (drafts, no numbers); new
approved time after the proposal -> 409 ``proposal_stale``; issuing has no tool; "Prepare invoice run" endpoint."""

from datetime import date

import pytest
from sqlalchemy import select

from app.core.agentic import Kickoff, get_agent, tools
from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.models.billing import Invoice, InvoiceRun
from app.models.notifications import Notification
from app.services import ai
from tests.agents.finance.helpers import call, calls, decide, outcomes, proposals, run_agent
from tests.modules.billing.conftest import BOrg, hours, placement

PREVIEW = "billing.preview_run"
UNAPPROVED = "billing.list_unapproved_time"
RATES = "billing.check_rates"
FLAG = "billing.flag_exception"
PREPARE = "billing.prepare_run_draft"
START, END = date(2026, 9, 1), date(2026, 9, 30)


async def _enable(org: BOrg) -> None:
    async with tenant_session(org.tenant_id) as db:
        await ai.set_features(db, org.tenant_id, {"agentic": True, "agent_billing": True})


def _kickoff(org: BOrg) -> Kickoff:
    return Kickoff(inputs={"legal_entity_id": str(org.entity_id), "period_start": START.isoformat(), "period_end": END.isoformat(), "client_id": "none"})


def _script(org: BOrg, pid: object) -> list[dict[str, object]]:
    period = {"legal_entity_id": str(org.entity_id), "period_start": START.isoformat(), "period_end": END.isoformat()}
    return [
        call(PREVIEW, period),
        call(UNAPPROVED, period),
        call(RATES, {"legal_entity_id": str(org.entity_id), "placement_ids": [str(pid)]}),
        call(FLAG, {"client_id": str(org.client_id), "kind": "po_missing", "details": "No purchase order on file for September."}),
        call(PREPARE, period),
        {"kind": "emit_result", "result": {"outcome": "proposed", "summary": "Run ready to start", "period": "Sep 2026"}},
    ]


def test_definition_never_issues() -> None:
    spec = get_agent("billing")
    assert not spec.placeholder and spec.subject_type == "legal_entity"
    registered = tools()
    assert registered[PREPARE].risk == "high" and registered[PREPARE].default_mode == "suggest"
    assert not any(k for k in spec.tools if any(w in k for w in ("issue", "send", "number", "credit", "payment")))


async def test_prepare_propose_approve_drafts_without_numbers(borg: BOrg) -> None:
    org = borg
    await _enable(org)
    pid = await placement(org)
    hours(org, pid, "8", date(2026, 9, 1))
    hours(org, pid, "8", date(2026, 9, 2))
    ref = await run_agent(org.tenant_id, org.admin.user_id, "billing", ("legal_entity", org.entity_id), _script(org, pid), kickoff=_kickoff(org))
    got = await calls(org.tenant_id, ref.run_id)
    assert [o[:2] for o in outcomes(got)] == [
        (PREVIEW, "executed"), (UNAPPROVED, "executed"), (RATES, "executed"), (FLAG, "executed"), (PREPARE, "proposed"),
    ], outcomes(got)
    preview = next(c for c in got if c.tool_key == PREVIEW)
    assert preview.risk == "read"
    (prop,) = await proposals(org.tenant_id, ref.run_id)
    assert prop.preview["title"].startswith("Start invoice run for") and "1 draft invoices" in prop.preview["title"]
    async with tenant_session(org.tenant_id) as db:
        assert list((await db.execute(select(InvoiceRun))).scalars()) == []  # nothing until a person approves
        notes = list((await db.execute(select(Notification).where(Notification.type == "invoice_run_prepared"))).scalars())
    assert any(n.user_id == org.admin.user_id for n in notes)
    out = await decide(org.tenant_id, org.admin.user_id, prop.id)
    assert out.status == "executed", out
    async with tenant_session(org.tenant_id) as db:
        (run,) = list((await db.execute(select(InvoiceRun))).scalars())
        drafts = list((await db.execute(select(Invoice).where(Invoice.invoice_run_id == run.id))).scalars())
    assert run.created_by == org.admin.user_id and run.status == "built"
    assert drafts and all(d.number is None and d.status == "draft" for d in drafts)


async def test_new_approved_time_makes_the_proposal_stale(borg: BOrg) -> None:
    org = borg
    await _enable(org)
    pid = await placement(org)
    hours(org, pid, "8", date(2026, 9, 1))
    ref = await run_agent(org.tenant_id, org.admin.user_id, "billing", ("legal_entity", org.entity_id), _script(org, pid), kickoff=_kickoff(org))
    (prop,) = await proposals(org.tenant_id, ref.run_id)
    hours(org, pid, "6", date(2026, 9, 3))  # new approved time arrived
    with pytest.raises(ConflictError) as exc:
        await decide(org.tenant_id, org.admin.user_id, prop.id)
    assert exc.value.code == "proposal_stale"


async def test_issue_is_not_a_tool_and_prepare_endpoint(borg: BOrg) -> None:
    org = borg
    await _enable(org)
    ref = await run_agent(
        org.tenant_id, org.admin.user_id, "billing", ("legal_entity", org.entity_id),
        [call("billing.issue_invoice", {"invoice_id": str(org.client_id)}), {"kind": "emit_result", "result": {"outcome": "nothing_to_do", "summary": "x"}}],
        kickoff=_kickoff(org),
    )  # fmt: skip
    (c,) = await calls(org.tenant_id, ref.run_id)
    assert (c.outcome, c.error_code) == ("policy_violation", "not_allowlisted")
    from app.services.ai.fake_llm import fake_llm
    from tests.agents.finance.helpers import turns

    with fake_llm(turns({"kind": "emit_result", "result": {"outcome": "nothing_to_do", "summary": "nothing billable"}})):
        resp = await org.c.post(
            "/api/v1/billing/prepare-run",
            json={"legal_entity_id": str(org.entity_id), "period_start": START.isoformat(), "period_end": END.isoformat()},
            headers=org.h(),
        )  # fmt: skip
    assert resp.status_code == 200, resp.text
    assert resp.json()["run_id"] is not None
    bad = await org.c.post(
        "/api/v1/billing/prepare-run",
        json={"legal_entity_id": str(org.entity_id), "period_start": END.isoformat(), "period_end": START.isoformat()}, headers=org.h(),
    )
    assert bad.status_code == 422

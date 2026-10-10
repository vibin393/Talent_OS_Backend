"""Expenses end to end (Epic 27): policies + versions, Worker claims (receipts, FX, mileage, flags,
justification), per-line approval on the placement route (AM -> Client -> Finance), Client step skipping,
exactly-once posting, ledger locks, consumer hooks (``mark_reimbursed`` / ``mark_stated``), isolation and AI
receipt read."""

import asyncio
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.services import expenses_service, postings
from app.services.postings import SourceQuery, load_unposted
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.expenses.helpers import (
    CLIENT,
    EXP,
    RECEIPTS,
    STANDARD,
    WORK,
    add_line,
    cat,
    decide,
    detail,
    fx,
    line_by,
    new_claim,
    ok_line,
    policy,
    post_payable,
    submit,
    upload_receipt,
)
from tests.modules.time.helpers import AM, CLIENT_ANY, CSRF, FINANCE, Setup, active, client_user


def _no_floats(payload: Any) -> None:
    if isinstance(payload, float):
        raise AssertionError(f"float in payload: {payload}")
    if isinstance(payload, dict):
        for v in payload.values():
            _no_floats(v)
    if isinstance(payload, list):
        for v in payload:
            _no_floats(v)


async def _loader(org: DOrg, s: Setup, ledger: str) -> list[postings.SourceItem]:
    async with tenant_session(org.tenant_id) as db:
        items = await load_unposted(
            admin_ctx(org),
            db,
            SourceQuery(ledger=ledger, period_end=date.today(), placement_ids=[uuid.UUID(s.pid)]),  # type: ignore[arg-type]
        )
    return [i for i in items if i.source_kind == "expense_line"]


async def test_acceptance_flow(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM, CLIENT_ANY, FINANCE])
    await policy(dorg, *STANDARD)
    cu = await client_user(app, dorg)
    d1 = s.start + timedelta(days=1)
    await fx(dorg, "USD", "INR", d1, "83.123456")
    claim = await new_claim(s)
    assert claim["status"] == "draft" and claim["currency"] == "INR"
    receipt = await upload_receipt(s)
    assert receipt.status_code == 201, receipt.text
    rid = receipt.json()["file_id"]
    # preview for mileage: amount computed from km x rate (entered amount ignored)
    prev = await s.worker.post(
        f"{WORK}/{claim['id']}/lines/preview",
        json={"expense_date": d1.isoformat(), "category": "mileage", "km": "37.5", "amount": "1.00"},
        headers=CSRF,
    )
    assert prev.status_code == 200 and prev.json()["amount_claim_ccy"] == "356.25", prev.text
    await ok_line(
        s,
        claim["id"],
        expense_date=d1.isoformat(),
        category="travel",
        amount="12.35",
        currency="USD",
        receipt_file_id=rid,
        description="Taxi to client",
    )
    await ok_line(s, claim["id"], expense_date=d1.isoformat(), category="mileage", km="37.5")
    out = await ok_line(s, claim["id"], expense_date=d1.isoformat(), category="meals", amount="2100.00")
    _no_floats(out)
    assert "bill_amount" not in str(out) and "markup" not in str(out)
    taxi, mileage, meals = line_by(out, "travel"), line_by(out, "mileage"), line_by(out, "meals")
    assert (taxi["amount_claim_ccy"], taxi["fx_rate"], taxi["billable"]) == ("1026.57", "83.123456", True)
    assert (mileage["amount_claim_ccy"], mileage["billable"]) == ("356.25", False)
    assert meals["amount_claim_ccy"] == "2100.00" and meals["flags"] == ["over_daily_limit"]
    assert out["total_claimed"] == "3482.82"
    # flagged line without justification -> 422 with the line id
    resp = await submit(s, claim["id"])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "justification_required"
    assert resp.json()["error"]["details"]["line_ids"] == [meals["id"]]
    resp = await s.worker.put(
        f"{WORK}/{claim['id']}/lines/{meals['id']}",
        json={
            "expense_date": d1.isoformat(),
            "category": "meals",
            "amount": "2100.00",
            "justification": "Team dinner with client",
        },
        headers=CSRF,
    )
    assert resp.status_code == 200, resp.text
    resp = await submit(s, claim["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "submitted" and resp.json()["can_recall"] is True
    # AM approves -> in_approval
    assert (await decide(dorg, claim["id"])).status_code == 200
    d = await detail(dorg, claim["id"])
    assert d["status"] == "in_approval" and d["current_step"] == "Client"
    # Client sees billable lines only; no markup / non-billable lines / reimbursement totals
    listed = await cu.client.get(CLIENT)
    assert listed.status_code == 200 and [c["id"] for c in listed.json()] == [claim["id"]]
    cd = (await cu.client.get(f"{CLIENT}/{claim['id']}")).json()
    assert [ln["id"] for ln in cd["lines"]] == [taxi["id"]]
    assert cd["billable_total"] == "1026.57" and cd["lines"][0]["bill_amount"] == "1026.57"
    for forbidden in ("markup", "total_claimed", "reimbursable", "356.25", "2100"):
        assert forbidden not in str(cd)
    resp = await cu.client.post(
        f"{CLIENT}/{claim['id']}/decide",
        json={
            "action": "approve",
            "line_decisions": [{"line_id": mileage["id"], "decision": "reject", "comment": "x"}],
        },
        headers=cu.h(),
    )
    assert resp.status_code == 422, resp.text  # a non-billable line is not the Client's to decide
    resp = await cu.client.post(f"{CLIENT}/{claim['id']}/decide", json={"action": "approve"}, headers=cu.h())
    assert resp.status_code == 200, resp.text
    # Finance rejects meals without a comment -> 422; with a comment -> partially approved
    resp = await decide(dorg, claim["id"], line_decisions=[{"line_id": meals["id"], "decision": "reject"}])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "comment_required"
    resp = await decide(
        dorg,
        claim["id"],
        line_decisions=[{"line_id": meals["id"], "decision": "reject", "comment": "Over policy"}],
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    _no_floats(d)
    assert d["status"] == "partially_approved" and d["total_approved"] == "1382.82"
    assert (
        line_by(d, "meals")["line_status"] == "rejected"
        and line_by(d, "meals")["decision_comment"] == "Over policy"
    )
    assert (
        line_by(d, "travel")["bill_amount"] == "1026.57"
        and line_by(d, "travel")["rebill_method"] == "at_cost"
    )
    assert line_by(d, "mileage")["bill_amount"] is None
    steps = [(x["name"], x["outcome"], x["actor_kind"]) for x in d["approval_history"]]
    assert steps == [
        ("Account Manager", "approved", "user"),
        ("Client", "approved", "client_user"),
        ("Finance", "approved", "user"),
    ]
    assert [x["decision"] for x in line_by(d, "mileage")["decisions"]] == ["approved", "passed", "approved"]
    # worker view shows the comment, never bill amounts
    w = (await s.worker.get(f"{WORK}/{claim['id']}")).json()
    assert line_by(w, "meals")["decision_comment"] == "Over policy" and "bill_amount" not in str(w)
    # loaders: payable = approved reimbursable lines; billing = billable only
    payable = await _loader(dorg, s, "payable")
    assert {str(i.source_id) for i in payable} == {taxi["id"], mileage["id"]}
    assert {str(i.source_id): str(i.amount) for i in payable} == {
        taxi["id"]: "1026.57",
        mileage["id"]: "356.25",
    }
    billing = await _loader(dorg, s, "billing")
    assert [str(i.source_id) for i in billing] == [taxi["id"]]
    assert billing[0].attributes["unit"] == "item" and billing[0].attributes["rebill_tax_mode"] == "taxable"
    # payroll finalize posts both, then mark processed -> reimbursed (idempotent)
    await post_payable(dorg, [taxi["id"]], s.pid)
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await expenses_service.mark_reimbursed(
                admin_ctx(dorg), db, [uuid.UUID(taxi["id"])], uuid.uuid4(), "PR-1"
            )
            == []
        )
    assert (await detail(dorg, claim["id"]))["status"] == "partially_approved"  # one line still unposted
    await post_payable(dorg, [mileage["id"]], s.pid)
    version = uuid.uuid4()
    async with tenant_session(dorg.tenant_id) as db:
        ids = [uuid.UUID(taxi["id"]), uuid.UUID(mileage["id"])]
        assert await expenses_service.mark_reimbursed(admin_ctx(dorg), db, ids, version, "PR-1") == [
            uuid.UUID(claim["id"])
        ]
    async with tenant_session(dorg.tenant_id) as db:
        assert await expenses_service.mark_reimbursed(admin_ctx(dorg), db, ids, version, "PR-1") == []
    d = await detail(dorg, claim["id"])
    assert d["status"] == "reimbursed" and d["payroll_reference"] == "PR-1" and d["locked"] is True
    resp = await dorg.admin.client.post(
        f"{EXP}/claims/{claim['id']}/reopen", json={"reason": "fix"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "claim_locked"
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Exception, match="document_immutable"):
            async with db.begin_nested():
                await db.execute(
                    text("UPDATE expense_lines SET amount = 1 WHERE id = :i"), {"i": uuid.UUID(taxi["id"])}
                )


async def test_client_step_skipped_and_rejections(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM, CLIENT_ANY, FINANCE])
    await policy(dorg, *STANDARD)
    d1 = s.start + timedelta(days=1)
    # no billable line -> no Client step at all
    claim = await new_claim(s)
    await ok_line(s, claim["id"], expense_date=d1.isoformat(), category="other", amount="100.00")
    assert (await submit(s, claim["id"])).status_code == 200
    assert (await decide(dorg, claim["id"])).status_code == 200
    assert (await detail(dorg, claim["id"]))["current_step"] == "Finance"
    # recall is refused after a decision; whole-claim reject needs a comment
    assert (await s.worker.post(f"{WORK}/{claim['id']}/recall", headers=CSRF)).status_code == 409
    assert (await decide(dorg, claim["id"], "reject")).status_code == 422
    resp = await decide(dorg, claim["id"], "reject", "Missing receipts")
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert resp.json()["rejection_comment"] == "Missing receipts"
    # Worker edits the rejected claim -> draft; resubmission restarts the route
    out = await ok_line(s, claim["id"], expense_date=d1.isoformat(), category="other", amount="5.00")
    assert out["status"] == "draft" and all(ln["line_status"] == "pending" for ln in out["lines"])
    assert (await submit(s, claim["id"])).status_code == 200
    assert (await detail(dorg, claim["id"]))["current_step"] == "Account Manager"
    # billable line rejected by the AM -> the Client step is skipped (recorded, actor system)
    c2 = await new_claim(s)
    taxi = line_by(
        await ok_line(s, c2["id"], expense_date=d1.isoformat(), category="travel", amount="300.00"), "travel"
    )
    await ok_line(s, c2["id"], expense_date=d1.isoformat(), category="other", amount="50.00")
    assert (await submit(s, c2["id"])).status_code == 200
    resp = await decide(
        dorg,
        c2["id"],
        line_decisions=[{"line_id": taxi["id"], "decision": "reject", "comment": "Personal trip"}],
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["current_step"] == "Finance" and d["status"] == "in_approval"
    assert ("Client", "skipped", "system") in [
        (x["name"], x["outcome"], x["actor_kind"]) for x in d["approval_history"]
    ]
    # an earlier rejection is final
    resp = await decide(dorg, c2["id"], line_decisions=[{"line_id": taxi["id"], "decision": "approve"}])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "line_already_rejected"
    resp = await decide(dorg, c2["id"])
    assert resp.status_code == 200 and resp.json()["status"] == "partially_approved"
    # every line rejected at the first step -> the remaining steps are skipped and the claim is rejected
    c3 = await new_claim(s)
    only = line_by(
        await ok_line(s, c3["id"], expense_date=d1.isoformat(), category="other", amount="10.00"), "other"
    )
    assert (await submit(s, c3["id"])).status_code == 200
    resp = await decide(
        dorg, c3["id"], line_decisions=[{"line_id": only["id"], "decision": "reject", "comment": "No"}]
    )
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    # Worker recall before any decision
    c4 = await new_claim(s)
    await ok_line(s, c4["id"], expense_date=d1.isoformat(), category="other", amount="10.00")
    assert (await submit(s, c4["id"])).status_code == 200
    resp = await s.worker.post(f"{WORK}/{c4['id']}/recall", headers=CSRF)
    assert resp.status_code == 200 and resp.json()["status"] == "draft"


async def test_validation_edge_cases(app: FastAPI, dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await active(app, dorg, route=[AM])
    await policy(dorg, *STANDARD, cat("equipment", enabled=False))
    d1 = s.start + timedelta(days=1)
    claim = await new_claim(s)

    async def code(**line: Any) -> tuple[int, str]:
        resp = await add_line(s, claim["id"], **line)
        body = resp.json()
        return resp.status_code, (body.get("error") or {}).get("code", "")

    assert await code(expense_date=d1.isoformat(), category="travel", amount="10.00", currency="EUR") == (
        422,
        "fx_rate_missing",
    )
    assert await code(expense_date=d1.isoformat(), category="travel", amount="10.000", currency="KWD") == (
        422,
        "currency_unsupported",
    )
    assert (await code(expense_date=d1.isoformat(), category="travel", amount=10.5))[0] == 422
    assert await code(expense_date=d1.isoformat(), category="travel", amount="-1.00") == (
        422,
        "negative_amount",
    )
    assert await code(
        expense_date=(s.start - timedelta(days=3)).isoformat(), category="travel", amount="1.00"
    ) == (422, "expense_date_outside_placement")
    assert await code(
        expense_date=(date.today() + timedelta(days=2)).isoformat(), category="travel", amount="1.00"
    ) == (422, "expense_date_outside_placement")
    # empty claim; disabled category
    resp = await submit(s, claim["id"])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "claim_empty"
    await ok_line(
        s, claim["id"], expense_date=d1.isoformat(), category="equipment", amount="10.00", justification="x"
    )
    resp = await submit(s, claim["id"])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "category_disabled"
    # infected receipt -> 422, not linked
    from app.core import scanning
    from app.core.scanning import ScanResult

    async def infected(data: bytes, filename: str) -> ScanResult:
        return ScanResult("infected", "Eicar")

    monkeypatch.setattr(scanning, "scan", infected)
    resp = await upload_receipt(s)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "receipt_infected"
    monkeypatch.undo()
    # duplicate receipt hash -> flag only (not a block)
    rid = (await upload_receipt(s)).json()["file_id"]
    rid2 = (await upload_receipt(s, name="again.pdf")).json()["file_id"]
    await ok_line(
        s, claim["id"], expense_date=d1.isoformat(), category="other", amount="1.00", receipt_file_id=rid
    )
    out = await ok_line(
        s, claim["id"], expense_date=d1.isoformat(), category="other", amount="2.00", receipt_file_id=rid2
    )
    flags = {ln["amount"]: ln["flags"] for ln in out["lines"] if ln["category"] == "other"}
    assert flags["2.00"] == ["possible_duplicate_receipt"]


async def test_placement_not_claimable(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM])
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            text("UPDATE placements SET status = 'ended', end_date = :d WHERE id = :i"),
            {"d": date.today() - timedelta(days=90), "i": uuid.UUID(s.pid)},
        )
    resp = await s.worker.post(WORK, json={"placement_id": s.pid}, headers=CSRF)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "placement_not_claimable"


async def test_policy_versions_and_client_override(app: FastAPI, dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM])
    d1 = s.start + timedelta(days=1)
    p = await policy(dorg, cat("meals", daily_limit="1000.00"))
    # a new version effective today: lines dated before it still use version 1
    resp = await dorg.admin.client.post(
        f"{EXP}/policies/{p['id']}/versions",
        json={
            "effective_from": date.today().isoformat(),
            "categories": [cat("meals", daily_limit="5000.00")],
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 201 and resp.json()["version"] == 2, resp.text
    assert [v["version"] for v in resp.json()["versions"]] == [1, 2]
    claim = await new_claim(s)
    out = await ok_line(s, claim["id"], expense_date=d1.isoformat(), category="meals", amount="1500.00")
    assert line_by(out, "meals")["flags"] == ["over_daily_limit"]
    old = await dorg.admin.client.get(f"{EXP}/policies/{p['id']}", params={"version": 1})
    assert old.json()["categories"][0]["daily_limit"] == "1000.00"
    # Client override beats the tenant policy
    resp = await dorg.admin.client.post(
        f"{EXP}/policies",
        json={
            "name": "Acme",
            "scope": "client",
            "client_id": str(dorg.client_id),
            "categories": [cat("meals", daily_limit="2000.00")],
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 201, resp.text
    out = await ok_line(
        s, claim["id"], expense_date=(d1 + timedelta(days=1)).isoformat(), category="meals", amount="1500.00"
    )
    flags = [ln["flags"] for ln in out["lines"] if ln["expense_date"] == (d1 + timedelta(days=1)).isoformat()]
    assert flags == [[]]
    dup = await dorg.admin.client.post(
        f"{EXP}/policies", json={"name": "Again", "categories": []}, headers=h(dorg.admin)
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "policy_exists"


async def _approved_claim(app: FastAPI, org: DOrg, **kw: Any) -> tuple[Setup, dict[str, Any]]:
    s = await active(app, org, route=[AM], **kw)
    await policy(org, *STANDARD)
    d1 = s.start + timedelta(days=1)
    claim = await new_claim(s)
    await ok_line(s, claim["id"], expense_date=d1.isoformat(), category="travel", amount="100.00")
    await ok_line(s, claim["id"], expense_date=d1.isoformat(), category="accommodation", amount="333.33")
    assert (await submit(s, claim["id"])).status_code == 200
    resp = await decide(org, claim["id"])
    assert resp.status_code == 200 and resp.json()["status"] == "approved", resp.text
    return s, resp.json()


async def test_exactly_once_posting_and_locks(app: FastAPI, dorg: DOrg) -> None:
    s, d = await _approved_claim(app, dorg)
    hotel = line_by(d, "accommodation")
    assert hotel["bill_amount"] == "366.66" and hotel["markup_pct"] == "10.0000"
    lid = uuid.UUID(hotel["id"])
    ctx = admin_ctx(dorg)
    # reopen while free -> draft, then resubmit / approve again
    resp = await dorg.admin.client.post(
        f"{EXP}/claims/{d['id']}/reopen", json={"reason": "typo"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["status"] == "draft"
    assert (await submit(s, d["id"])).status_code == 200
    assert (await decide(dorg, d["id"])).json()["status"] == "approved"
    # one payable + one billing posting of the same line is allowed; a second payable -> 409
    await post_payable(dorg, [hotel["id"]], s.pid)
    async with tenant_session(dorg.tenant_id) as db:
        await postings.reserve(
            ctx,
            db,
            "billing",
            [
                postings.PostingLine(
                    "expense_line",
                    lid,
                    "invoice_line",
                    uuid.uuid4(),
                    None,
                    postings.Decimal("1"),
                    postings.Decimal("366.66"),
                    "INR",
                    uuid.UUID(s.pid),
                )
            ],
        )
    with pytest.raises(ConflictError) as exc:
        await post_payable(dorg, [hotel["id"]], s.pid)
    assert exc.value.code == "source_already_posted"
    assert hotel["id"] not in {str(i.source_id) for i in await _loader(dorg, s, "payable")}
    resp = await dorg.admin.client.post(
        f"{EXP}/claims/{d['id']}/reopen", json={"reason": "x"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "claim_locked"
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Exception, match="document_immutable"):
            async with db.begin_nested():
                await db.execute(text("DELETE FROM expense_lines WHERE id = :i"), {"i": lid})
    # release + repost -> one active
    async with tenant_session(dorg.tenant_id) as db:
        active_rows = await postings.active_postings_for(db, "expense_line", [lid], ledger="payable")
        await postings.release(
            ctx, db, "payable", reason="void", posting_ids=[p.id for p in active_rows[lid]]
        )
    await post_payable(dorg, [hotel["id"]], s.pid)
    async with tenant_session(dorg.tenant_id) as db:
        rows = await postings.targets_for(db, "payable", "expense_line", lid)
    assert sorted(r.state for r in rows) == ["posted", "released"]
    # concurrent finalize simulations on another line -> exactly one active posting
    taxi = line_by(d, "travel")

    async def one() -> bool:
        try:
            await post_payable(dorg, [taxi["id"]], s.pid)
            return True
        except Exception:
            return False

    results = await asyncio.gather(*(one() for _ in range(20)))
    assert results.count(True) == 1


async def test_vendor_payee_and_mark_stated(app: FastAPI, dorg: DOrg) -> None:
    s, d = await _approved_claim(app, dorg)
    # payee routing is the consumer's: a worker-payee placement never appears in a vendor query
    async with tenant_session(dorg.tenant_id) as db:
        items = await load_unposted(
            admin_ctx(dorg), db, SourceQuery(ledger="payable", period_end=date.today(), payee_kind="vendor")
        )
    assert not [i for i in items if i.source_kind == "expense_line" and str(i.placement_id) == s.pid]
    ids = [ln["id"] for ln in d["lines"]]
    await post_payable(dorg, ids, s.pid, payee="vendor")
    async with tenant_session(dorg.tenant_id) as db:
        done = await expenses_service.mark_stated(
            admin_ctx(dorg), db, [uuid.UUID(i) for i in ids], uuid.uuid4()
        )
        again = await expenses_service.mark_stated(
            admin_ctx(dorg), db, [uuid.UUID(i) for i in ids], uuid.uuid4()
        )
    assert done == [uuid.UUID(d["id"])] and again == []
    assert (await detail(dorg, d["id"]))["status"] == "stated"
    # offboarding verifier: settled
    async with tenant_session(dorg.tenant_id) as db:
        result = await expenses_service.final_expense_settlement(db, uuid.UUID(s.pid), date.today())
    assert result.done is True  # type: ignore[attr-defined]


async def test_isolation(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    s = await active(app, dorg, route=[AM, CLIENT_ANY])
    await policy(dorg, *STANDARD)
    d1 = s.start + timedelta(days=1)
    claim = await new_claim(s)
    await ok_line(s, claim["id"], expense_date=d1.isoformat(), category="travel", amount="100.00")
    assert (await submit(s, claim["id"])).status_code == 200
    rid = (await upload_receipt(s)).json()["file_id"]
    # another Worker of the same tenant
    s2 = await active(app, dorg, route=[AM])
    assert (await s2.worker.get(f"{WORK}/{claim['id']}")).status_code == 404
    assert (await s2.worker.post(f"{WORK}/{claim['id']}/recall", headers=CSRF)).status_code == 404
    assert (await s2.worker.get(f"{RECEIPTS}/{rid}")).status_code == 404
    assert (await s.worker.get(f"{RECEIPTS}/{rid}")).status_code == 200
    assert (await s2.worker.get(WORK)).json() == []
    # another tenant
    assert (await other_dorg.admin.client.get(f"{EXP}/claims/{claim['id']}")).status_code == 404
    assert (await other_dorg.admin.client.get(f"{EXP}/claims")).json() == []
    # a Client User of another Client -> 404
    from tests.modules.demands.helpers import create_client
    from tests.p2 import make_client_user, sign_in_external

    other_client = await create_client(dorg.admin)
    cu_other = await sign_in_external(
        app, "client_user", await make_client_user(dorg.tenant_id, other_client), dorg.tenant_id, other_client
    )
    assert (await cu_other.client.get(f"{CLIENT}/{claim['id']}")).status_code == 404
    # restricted Client User without scope -> 404
    cu_restricted = await client_user(app, dorg, restricted=True)
    assert (await cu_restricted.client.get(f"{CLIENT}/{claim['id']}")).status_code == 404
    cu = await client_user(app, dorg)
    assert (await cu.client.get(f"{CLIENT}/{claim['id']}")).status_code == 200


async def test_ai_receipt_read(app: FastAPI, dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.expenses import receipts
    from tests.epic3 import make_pdf

    s = await active(app, dorg, route=[AM])
    rid = (await upload_receipt(s, make_pdf("Cafe Coffee Day 2026-10-01 Total 1,234.50 INR"))).json()[
        "file_id"
    ]
    resp = await s.worker.post(f"{RECEIPTS}/{rid}/read", headers=CSRF)
    assert resp.status_code == 404  # disabled by default
    resp = await dorg.admin.client.patch(
        f"{EXP}/settings", json={"ai_receipt_read_enabled": True}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["ai_receipt_read_enabled"] is True

    class Result:
        def __init__(self, ok: bool, data: Any = None) -> None:
            self.ok, self.data, self.error_code = ok, data, None if ok else "provider_error"

    async def failing(*a: Any, **k: Any) -> Result:
        return Result(False)

    monkeypatch.setattr(receipts, "_run_ai", failing)
    resp = await s.worker.post(f"{RECEIPTS}/{rid}/read", headers=CSRF)
    assert resp.status_code == 200 and resp.json()["ok"] is False and resp.json()["suggestion"] is None

    async def reading(*a: Any, **k: Any) -> Result:
        return Result(
            True,
            receipts.ReceiptRead(
                date="2026-10-01", merchant="CCD", amount="1,234.50", currency="inr", category_guess="meals"
            ),
        )

    monkeypatch.setattr(receipts, "_run_ai", reading)
    resp = await s.worker.post(f"{RECEIPTS}/{rid}/read", headers=CSRF)
    body = resp.json()
    assert body["ok"] is True and body["suggestion"]["amount"] is None
    assert body["suggestion"]["currency"] == "INR" and body["suggestion"]["category"] == "meals"

"""Story 12.2 (FR-68): DSR state machine, legal holds, erasure runs with the handler registry, crypto-shredded
audit PII, erasure ledger, access exports and due-date alerts. Matrix rows 5-8, 10, 11, 35."""

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core import audit
from app.core.database import tenant_session
from app.core.pagination import PageParams
from app.models.ai import AIAction
from app.models.candidates import CandidateEmail, CandidateResume
from app.models.files import StoredFile
from app.models.privacy import DsrRequest, ErasureRun
from app.services.privacy import registry
from app.services.privacy.erasure import read_ledger
from tests.epic12 import (
    CANDIDATES,
    PRIVACY,
    FakeAI,
    Org3,
    candidate_row,
    create_candidate,
    h,
    member,
    resume_pdf,
    sent,
    upload,
)

DSRS = f"{PRIVACY}/dsrs"


async def _rich_candidate(org: Org3) -> dict[str, Any]:
    cand = await create_candidate(
        org.admin,
        full_name="Meera Krishnan",
        emails=[{"email": "meera@example.com", "is_primary": True}],
        phones=[{"phone": "+91 98765 43210"}],
        current_title="Data Engineer",
        current_employer="Infosys",
        employment=[{"employer": "Infosys", "title": "Data Engineer"}],
    )
    resp = await upload(org.admin.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf())
    assert resp.status_code == 201, resp.text
    att = await upload(
        org.admin.client, f"/api/v1/candidates/{cand['id']}/attachments", b"%PDF-1.4 cert", "cert.pdf", category="certificate", note="Meera's certificate"
    )
    assert att.status_code == 201, att.text
    note = await org.admin.client.post(
        f"/api/v1/candidates/{cand['id']}/activities", json={"body": "Called Meera about the role"}, headers=h(org.admin)
    )
    assert note.status_code == 201, note.text
    return cand


async def _dsr(org: Org3, candidate_id: str | None, kind: str, **extra: Any) -> dict[str, Any]:
    body = {"candidate_id": candidate_id, "kind": kind, "channel": "email", "requester_email": "meera@example.com", **extra}
    resp = await org.admin.client.post(DSRS, json=body, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _post(org: Org3, url: str, body: dict[str, Any] | None = None) -> Any:
    return await org.admin.client.post(url, json=body or {}, headers=h(org.admin))


async def test_state_machine_and_refusal(org3: Org3) -> None:
    dsr = await _dsr(org3, None, "access", requester_name="Walk-in")
    assert dsr["human_id"].startswith("DSR-")
    assert dsr["status"] == "received"
    due = datetime.fromisoformat(dsr["due_at"]) - datetime.fromisoformat(dsr["received_at"])
    assert due == timedelta(days=30)
    bad = await _post(org3, f"{DSRS}/{dsr['id']}/transition", {"to": "completed"})
    assert (bad.status_code, bad.json()["error"]["code"]) == (409, "invalid_transition")
    short = await _post(org3, f"{DSRS}/{dsr['id']}/transition", {"to": "refused", "refusal_reason": "too short"})
    assert short.status_code == 422
    ok = await _post(org3, f"{DSRS}/{dsr['id']}/transition", {"to": "refused", "refusal_reason": "Requester could not be identified"})
    assert ok.json()["status"] == "refused"
    assert [e["action"] for e in ok.json()["timeline"]][:2] == ["dsr_request.created", "dsr.received"]
    assert (await _post(org3, f"{DSRS}/{dsr['id']}/transition", {"to": "in_progress"})).status_code == 409


async def test_correction_and_consent_withdrawal(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    corr = await _dsr(org3, cand["id"], "correction")
    resp = await _post(org3, f"{DSRS}/{corr['id']}/execute")
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "use_candidate_edit")
    withdraw = await _dsr(org3, cand["id"], "consent_withdrawal")
    done = await _post(org3, f"{DSRS}/{withdraw['id']}/execute", {"purposes": ["ai_processing"]})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    assert (await candidate_row(org3.tenant_id, cand["id"])).ai_consent_state == "withdrawn"


async def test_erasure_requires_verified_identity_and_no_hold(org3: Org3) -> None:
    """Matrix 5 and 6."""
    cand = await create_candidate(org3.admin)
    dsr = await _dsr(org3, cand["id"], "erasure")
    resp = await _post(org3, f"{DSRS}/{dsr['id']}/execute")
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "identity_not_verified")
    await _post(org3, f"{DSRS}/{dsr['id']}/verify-identity")
    hold = await _post(org3, f"{PRIVACY}/legal-holds", {"candidate_id": cand["id"], "reason": "Litigation 2026/14"})
    assert hold.status_code == 201, hold.text
    resp = await _post(org3, f"{DSRS}/{dsr['id']}/execute")
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "legal_hold_active")
    assert resp.json()["error"]["details"]["reason"] == "Litigation 2026/14"
    detail = (await org3.admin.client.get(f"{DSRS}/{dsr['id']}")).json()
    assert detail["legal_hold"]["reason"] == "Litigation 2026/14"
    await _post(org3, f"{PRIVACY}/legal-holds/{hold.json()['id']}/release")
    resp = await _post(org3, f"{DSRS}/{dsr['id']}/execute")
    assert resp.status_code == 200, resp.text


async def test_erasure_end_to_end(org3: Org3, fake_ai: FakeAI, _isolated_files: Path) -> None:
    """Matrix 7: handlers counted, verify clean, audit PII [erased], files deleted, ledger line, DSR completed."""
    cand = await _rich_candidate(org3)
    cid = uuid.UUID(cand["id"])
    async with tenant_session(org3.tenant_id) as db:
        keys = list((await db.execute(select(StoredFile.storage_key))).scalars())
        # An AI action with output about the candidate (resume parse).
        db.add(
            AIAction(
                tenant_id=org3.tenant_id, feature="resume_parsing", prompt_id="resume_parse", initiator_type="user",
                status="ok", subject_type="candidate", subject_id=cid, output={"full_name": "Meera Krishnan"},
                input_tokens=10, output_tokens=5,
            )
        )  # fmt: skip
    assert keys and all((_isolated_files / k).is_file() for k in keys)
    dsr = await _dsr(org3, cand["id"], "erasure")
    await _post(org3, f"{DSRS}/{dsr['id']}/verify-identity")
    resp = await _post(org3, f"{DSRS}/{dsr['id']}/execute", {"confirm": "ERASE"})
    assert resp.status_code == 200, resp.text
    detail = (await org3.admin.client.get(f"{DSRS}/{dsr['id']}")).json()
    assert detail["status"] == "completed", detail
    run = detail["erasure_run"]
    assert run["status"] == "completed"
    assert run["verification"] == {"findings": {}}
    results = run["handler_results"]
    assert {"candidates.profile", "files.candidate", "ai.actions", "activity.entity", "audit.shred"} <= set(results)
    assert results["candidates.profile"]["counts"]["candidate_emails"] == 1
    assert results["files.candidate"]["counts"]["files_deleted"] == len(keys)
    assert detail["requester_email"] is None  # requester PII cleared

    row = await candidate_row(org3.tenant_id, cid)
    assert (row.full_name, row.current_employer, row.ai_consent_state) == ("Erased candidate", None, "withdrawn")
    assert row.erased_at is not None and row.human_id == cand["human_id"]
    assert not any((_isolated_files / k).is_file() for k in keys)
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(select(CandidateEmail).where(CandidateEmail.candidate_id == cid))).first() is None
        resume = (await db.execute(select(CandidateResume).where(CandidateResume.candidate_id == cid))).scalar_one()
        assert resume.extracted_text is None
        actions = list((await db.execute(select(AIAction).where(AIAction.subject_id == cid))).scalars())
        assert actions and all((a.output, a.input_ref) == (None, "erased") for a in actions)
        assert 10 in {a.input_tokens for a in actions}  # metrics kept
        page = await audit.search(db, audit.AuditFilters(entity_type="candidate", entity_id=str(cid)), PageParams(page_size=100))
    names = [
        diff.get(side)
        for entry in page.items
        for field, diff in (entry.changes or {}).items()
        if field == "full_name"
        for side in ("old", "new")
    ]
    assert "Meera Krishnan" not in json.dumps([e.model_dump(mode="json") for e in page.items])
    assert "[erased]" in names
    ledger = await read_ledger((datetime.now(UTC) - timedelta(days=1)).date())
    assert {"tenant_id": str(org3.tenant_id), "candidate_id": str(cid)}.items() <= next(
        e for e in ledger if e["candidate_id"] == str(cid)
    ).items()
    assert "Meera" not in json.dumps(ledger)


async def test_failed_handler_is_retried_alone(org3: Org3) -> None:
    """Matrix 8: a failing handler fails the run; the retry re-runs only the failed handler."""
    calls: dict[str, int] = {"flaky": 0}

    class Flaky:
        name = "test.flaky"
        order = 15
        tables: tuple[str, ...] = ()

        async def erase(self, db: Any, ctx: Any) -> dict[str, int]:
            calls["flaky"] += 1
            if calls["flaky"] == 1:
                raise RuntimeError("boom")
            return {"rows": 0}

        async def export(self, db: Any, ctx: Any) -> None:
            return None

        async def verify(self, db: Any, ctx: Any) -> list[str]:
            return []

    registry.register_erasure_handler(Flaky())
    try:
        cand = await create_candidate(org3.admin)
        dsr = await _dsr(org3, cand["id"], "erasure")
        await _post(org3, f"{DSRS}/{dsr['id']}/verify-identity")
        await _post(org3, f"{DSRS}/{dsr['id']}/execute")
        run = (await org3.admin.client.get(f"{DSRS}/{dsr['id']}")).json()["erasure_run"]
        assert run["status"] == "failed"
        assert run["handler_results"]["test.flaky"]["status"] == "failed"
        profile_counts = run["handler_results"]["candidates.profile"]
        retry = await _post(org3, f"{PRIVACY}/erasure-runs/{run['id']}/retry")
        assert retry.status_code == 200, retry.text
        after = (await org3.admin.client.get(f"{DSRS}/{dsr['id']}")).json()
        assert after["status"] == "completed"
        assert after["erasure_run"]["handler_results"]["candidates.profile"] == profile_counts  # not re-run
        assert calls["flaky"] == 2
    finally:
        registry.unregister_erasure_handler("test.flaky")


async def test_access_export_zip_and_link(org3: Org3, _isolated_files: Path) -> None:
    """Matrix 11."""
    cand = await _rich_candidate(org3)
    dsr = await _dsr(org3, cand["id"], "access")
    await _post(org3, f"{DSRS}/{dsr['id']}/verify-identity")
    resp = await _post(org3, f"{DSRS}/{dsr['id']}/execute")
    assert resp.status_code == 200, resp.text
    detail = (await org3.admin.client.get(f"{DSRS}/{dsr['id']}")).json()
    assert detail["status"] == "completed"
    (mail,) = sent("dsr_export", to="meera@example.com")
    token = mail.text.split("/l/")[1].split()[0]
    dl = await org3.admin.client.get(f"/api/v1/public/links/{token}/download")
    assert dl.status_code == 200, dl.text
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        data = json.loads(zf.read("data.json"))
        names = zf.namelist()
    assert data["candidates.profile"]["profile"]["full_name"] == "Meera Krishnan"
    assert any(n.startswith("files/") for n in names)
    assert (_isolated_files / f"t/{org3.tenant_id}/dsr/{dsr['id']}.zip").is_file()


async def test_due_soon_and_overdue_alerts(org3: Org3) -> None:
    """Matrix 10: 23 days old -> one due-soon notice; 31 days old -> one overdue notice + badge."""
    now = datetime.now(UTC)
    a = await _dsr(org3, None, "access", received_at=(now - timedelta(days=23)).isoformat())
    b = await _dsr(org3, None, "erasure", received_at=(now - timedelta(days=31)).isoformat())
    from app.tasks.privacy import dsr_due_scan

    before = len(sent("dsr_due_alert"))
    dsr_due_scan.delay(str(org3.tenant_id))
    dsr_due_scan.delay(str(org3.tenant_id))  # idempotent: no second notice
    assert len(sent("dsr_due_alert")) == before + 1
    async with tenant_session(org3.tenant_id) as db:
        rows = {r.id: r for r in (await db.execute(select(DsrRequest))).scalars()}
    assert rows[uuid.UUID(a["id"])].due_soon_notified_at is not None
    assert rows[uuid.UUID(a["id"])].overdue_notified_at is None
    assert rows[uuid.UUID(b["id"])].overdue_notified_at is not None
    overdue = (await org3.admin.client.get(DSRS, params={"overdue": "true"})).json()["items"]
    assert [d["id"] for d in overdue] == [b["id"]]
    assert overdue[0]["overdue"] is True


async def test_permissions_grievance_officer_and_cross_tenant(app: FastAPI, org3: Org3, other3: Org3) -> None:
    """Matrix 35 for DSR / hold endpoints; the seeded Grievance Officer can process DSRs."""
    other_cand = await create_candidate(other3.admin)
    other_dsr = await _dsr(other3, other_cand["id"], "access")
    for method, url in [
        ("get", f"{DSRS}/{other_dsr['id']}"),
        ("patch", f"{DSRS}/{other_dsr['id']}"),
        ("post", f"{DSRS}/{other_dsr['id']}/verify-identity"),
        ("post", f"{DSRS}/{other_dsr['id']}/execute"),
        ("post", f"{DSRS}/{other_dsr['id']}/transition"),
        ("post", f"{PRIVACY}/legal-holds/{uuid.uuid4()}/release"),
        ("get", f"{PRIVACY}/erasure-runs/{uuid.uuid4()}"),
    ]:
        body = {"to": "in_progress"} if url.endswith("transition") else {}
        resp = await getattr(org3.admin.client, method)(url, **({} if method == "get" else {"json": body, "headers": h(org3.admin)}))
        assert resp.status_code == 404, (url, resp.text)
    resp = await _post(org3, DSRS, {"candidate_id": other_cand["id"], "kind": "access"})
    assert resp.status_code == 404
    resp = await _post(org3, f"{PRIVACY}/legal-holds", {"candidate_id": other_cand["id"], "reason": "x" * 5})
    assert resp.status_code == 404
    officer = await member(app, org3, "grievance_officer", name="Gita Officer")
    recruiter = await member(app, org3, "recruiter")
    try:
        assert (await officer.client.get(DSRS)).status_code == 200
        created = await officer.client.post(DSRS, json={"kind": "access", "requester_name": "X Y"}, headers=h(officer))
        assert created.status_code == 201
        assert (await recruiter.client.get(DSRS)).status_code == 403
    finally:
        await officer.client.aclose()
        await recruiter.client.aclose()


@pytest.mark.parametrize("confirm", ["nope"])
async def test_erasure_confirmation_must_match(org3: Org3, confirm: str) -> None:
    cand = await create_candidate(org3.admin)
    dsr = await _dsr(org3, cand["id"], "erasure")
    await _post(org3, f"{DSRS}/{dsr['id']}/verify-identity")
    resp = await _post(org3, f"{DSRS}/{dsr['id']}/execute", {"confirm": confirm})
    assert resp.status_code == 422
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(select(ErasureRun))).first() is None

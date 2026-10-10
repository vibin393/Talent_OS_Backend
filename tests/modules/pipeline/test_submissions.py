"""Submissions (6.4) and client feedback (6.5)."""

import base64
import io
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.database import tenant_session
from app.core.mailer import OutboxMailer
from app.models.demands import DemandSlaClock, SlaTarget
from app.models.pipeline import CandidateClientFirst, Submission
from tests.modules.identity.conftest import join
from tests.modules.pipeline.helpers import (
    API,
    POrg,
    add_one,
    create_candidate,
    h,
    links_in,
    per_app,
    rows,
    shortlist,
    stage_of,
    submit,
    upload_parsed_resume,
)


async def _ready(porg: POrg, name: str = "Priya Raman", parsed: bool = True) -> tuple[str, dict]:  # type: ignore[type-arg]
    cand = await create_candidate(porg.admin, name)
    await upload_parsed_resume(porg.admin, porg.tenant_id, cand["id"], parsed=parsed)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    await shortlist(porg.admin, app_id)
    return app_id, cand


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


async def test_submit_sends_email_with_links_and_clean_resume(porg: POrg, outbox: OutboxMailer) -> None:
    app_id, cand = await _ready(porg)
    per = {
        app_id: per_app(
            resume_kind="client_formatted",
            consent_attestation={"text": "Agreed by phone today", "confirmed": True},
        )
    }
    resp = await submit(porg, [app_id], per)
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["status"] == "sent", item
    assert await stage_of(porg.admin, app_id) == "submitted"

    sub = (await porg.admin.client.get(f"{API}/submissions/{item['submission_id']}")).json()
    assert sub["human_id"].startswith("SUB-") and sub["status"] == "sent"
    assert sub["margin_pct"] == "29.20" and sub["below_min_margin"] is False
    assert sub["consent_id"] and sub["resume_sha256"]

    mails = [m for m in outbox.sent if m.kind == "submission"]
    assert len(mails) == 1 and mails[0].to == porg.contact_email
    html = mails[0].html or ""
    import re

    visible = re.sub(r"<[^>]+>", " ", html)  # text content only (CSS "margin:" is not a leak)
    for secret in ("900", "margin", "Margin", "29.20"):
        assert secret not in visible and secret not in mails[0].text
    assert len(links_in(outbox, "submission")) == 3
    assert len(mails[0].attachments) == 1
    pdf = mails[0].attachments[0].data
    text = _pdf_text(pdf)
    assert "Priya Raman" in text and sub["human_id"] in text
    for leaked in ("@example.com", "98765", "9876543210", "linkedin"):
        assert leaked not in text

    # outbox JSON carries attachment metadata + base64
    import json

    path = outbox.outbox_path(mails[0])
    stored = json.loads(path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert stored["attachments"][0]["size"] == len(pdf)
    assert base64.b64decode(stored["attachments"][0]["data_base64"]) == pdf

    firsts = await rows(porg.tenant_id, select(CandidateClientFirst))
    assert len(firsts) == 1 and str(firsts[0].candidate_id) == cand["id"]
    demand = (await porg.admin.client.get(f"{API}/demands/{porg.demand_id}")).json()
    assert demand["submissions_count"] == 1 and demand["progress"] == "submitted"
    client360 = (await porg.admin.client.get(f"{API}/clients/{porg.client_id}/360")).json()
    assert (
        client360["counts"]["submissions"] == 1 and client360["submissions"][0]["id"] == item["submission_id"]
    )

    # Snapshot: later candidate edits do not change the stored resume.
    before = sub["resume_sha256"]
    await porg.admin.client.patch(
        f"{API}/candidates/{cand['id']}", json={"full_name": "Priya R"}, headers=h(porg.admin)
    )
    after = (await porg.admin.client.get(f"{API}/submissions/{item['submission_id']}")).json()
    assert after["resume_sha256"] == before


async def test_currency_mismatch_and_margin_preview(porg: POrg, app: FastAPI) -> None:
    app_id, _ = await _ready(porg)
    per = {app_id: {**per_app(), "pay_rate": {"amount": "10", "currency": "USD"}}}
    resp = await submit(porg, [app_id], per)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "currency_mismatch"

    preview = await porg.admin.client.post(
        f"{API}/submissions/margin-preview",
        json={
            "demand_id": porg.demand_id,
            "pay_rate": {"amount": "900", "currency": "INR"},
            "bill_rate": {"amount": "1500", "currency": "INR"},
        },
        headers=h(porg.admin),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["margin_amount"] == {"amount": "438.00", "currency": "INR"}

    recruiter = await join(app, porg.tenant, "recruiter", name="Rita Recruiter")
    denied = await recruiter.client.post(
        f"{API}/submissions/margin-preview",
        json={
            "pay_rate": {"amount": "900", "currency": "INR"},
            "bill_rate": {"amount": "1500", "currency": "INR"},
        },
        headers=h(recruiter),
    )
    assert denied.status_code == 403
    await recruiter.client.aclose()


async def test_below_minimum_needs_approval(porg: POrg, outbox: OutboxMailer) -> None:
    chain = await porg.admin.client.post(
        f"{API}/approval-chains",
        json={
            "subject_type": "margin_override",
            "name": "Margin",
            "steps": [{"name": "Head", "approver_user_id": str(porg.admin.user_id)}],
        },
        headers=h(porg.admin),
    )
    assert chain.status_code == 201, chain.text
    app_id, _ = await _ready(porg)
    per = {
        app_id: per_app(
            pay="1300.00", consent_attestation={"text": "Agreed by phone today", "confirmed": True}
        )
    }
    resp = await submit(porg, [app_id], per)
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["status"] == "pending_approval"
    assert not [m for m in outbox.sent if m.kind == "submission"]
    assert await stage_of(porg.admin, app_id) == "shortlisted"
    sub = (await porg.admin.client.get(f"{API}/submissions/{item['submission_id']}")).json()
    early = await porg.admin.client.post(f"{API}/submissions/{sub['id']}/send", headers=h(porg.admin))
    assert early.status_code == 409 and early.json()["error"]["code"] == "submission_not_approved"

    decided = await porg.admin.client.post(
        f"{API}/approvals/{sub['approval_request_id']}/decide",
        json={"action": "approve"},
        headers=h(porg.admin),
    )
    assert decided.status_code == 200, decided.text
    assert (await porg.admin.client.get(f"{API}/submissions/{sub['id']}")).json()["status"] == "approved"
    sent = await porg.admin.client.post(f"{API}/submissions/{sub['id']}/send", headers=h(porg.admin))
    assert sent.status_code == 200 and sent.json()["status"] == "sent"
    assert [m for m in outbox.sent if m.kind == "submission"]


async def test_guards_limit_prior_consent(porg: POrg) -> None:
    app_id, _ = await _ready(porg)
    # not shortlisted
    other = await add_one(
        porg.admin, porg.demand_id, (await create_candidate(porg.admin, "Rahul Sharma"))["id"]
    )
    blocked = await submit(porg, [other])
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "not_shortlisted"

    # consent: candidate_link mode refuses attestation
    await porg.admin.client.patch(
        f"{API}/pipeline/settings", json={"submission_consent_mode": "candidate_link"}, headers=h(porg.admin)
    )
    need = await submit(porg, [app_id])
    assert need.status_code == 409 and need.json()["error"]["code"] == "submission_consent_required"
    cand_id = (await porg.admin.client.get(f"{API}/applications/{app_id}")).json()["candidate"]["id"]
    req = await porg.admin.client.post(
        f"{API}/candidates/{cand_id}/consent-requests",
        json={"purpose": "client_submission", "client_id": str(porg.client_id)},
        headers=h(porg.admin),
    )
    assert req.status_code == 200 and req.json()["items"][0]["status"] == "sent"
    await porg.admin.client.patch(
        f"{API}/pipeline/settings",
        json={"submission_consent_mode": "attestation_allowed"},
        headers=h(porg.admin),
    )

    # submission limit
    async with tenant_session(porg.tenant_id) as db:
        from app.models.demands import Demand

        await db.execute(
            update(Demand).where(Demand.id == uuid.UUID(porg.demand_id)).values(submission_limit=0)
        )
    limited = await submit(porg, [app_id])
    assert limited.status_code == 409 and limited.json()["error"]["code"] == "submission_limit_reached"
    async with tenant_session(porg.tenant_id) as db:
        from app.models.demands import Demand

        await db.execute(
            update(Demand).where(Demand.id == uuid.UUID(porg.demand_id)).values(submission_limit=5)
        )

    # recipients must be contacts of the client
    bad = await submit(porg, [app_id], recipient_contact_ids=[str(uuid.uuid4())])
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_recipients"

    ok = await submit(porg, [app_id])
    assert ok.status_code == 200 and ok.json()["items"][0]["status"] == "sent"


async def test_prior_submission_reason(porg: POrg) -> None:
    from tests.modules.demands.helpers import open_demand

    app_id, cand = await _ready(porg)
    assert (await submit(porg, [app_id])).json()["items"][0]["status"] == "sent"
    async with tenant_session(porg.tenant_id) as db:
        await db.execute(update(Submission).values(sent_at=datetime.now(UTC) - timedelta(days=40)))
    d2 = await open_demand(porg.admin, porg.client_id, title="Data Lead")
    app2 = await add_one(porg.admin, str(d2["id"]), cand["id"])
    await shortlist(porg.admin, app2)
    body = {
        "application_ids": [app2],
        "per_application": {
            app2: per_app(consent_attestation={"text": "Agreed by phone today", "confirmed": True})
        },
        "recipient_contact_ids": [str(porg.contact_id)],
    }
    warn = await porg.admin.client.post(
        f"{API}/demands/{d2['id']}/submissions", json=body, headers=h(porg.admin)
    )
    assert warn.status_code == 409 and warn.json()["error"]["code"] == "prior_submission_warning"
    assert warn.json()["error"]["details"]["demand_id"] == porg.demand_id
    short = {
        **body,
        "per_application": {app2: {**body["per_application"][app2], "prior_submission_reason": "short"}},
    }
    assert (
        await porg.admin.client.post(
            f"{API}/demands/{d2['id']}/submissions", json=short, headers=h(porg.admin)
        )
    ).status_code == 422
    good = {
        **body,
        "per_application": {
            app2: {**body["per_application"][app2], "prior_submission_reason": "New role, different team"}
        },
    }
    resp = await porg.admin.client.post(
        f"{API}/demands/{d2['id']}/submissions", json=good, headers=h(porg.admin)
    )
    assert resp.status_code == 200 and resp.json()["items"][0]["status"] == "sent"


async def test_bulk_results_and_resume_not_parsed(porg: POrg) -> None:
    good, _ = await _ready(porg)
    unparsed, _ = await _ready(porg, "Rahul Sharma", parsed=False)
    per = {
        good: per_app(consent_attestation={"text": "Agreed by phone today", "confirmed": True}),
        unparsed: per_app(
            resume_kind="client_formatted",
            consent_attestation={"text": "Agreed by phone today", "confirmed": True},
        ),
    }
    resp = await submit(porg, [good, unparsed], per)
    assert resp.status_code == 200, resp.text
    items = {i["application_id"]: i for i in resp.json()["items"]}
    assert items[good]["status"] == "sent"
    assert items[unparsed]["status"] == "blocked" and items[unparsed]["error"]["code"] == "resume_not_parsed"


async def test_masking_for_recruiter(porg: POrg, app: FastAPI) -> None:
    app_id, _ = await _ready(porg)
    sub_id = (await submit(porg, [app_id])).json()["items"][0]["submission_id"]
    recruiter = await join(app, porg.tenant, "recruiter", name="Rita Recruiter")
    # the recruiter sees the demand through team/assignment scope only when assigned: grant via assignment
    await porg.admin.client.put(
        f"{API}/demands/{porg.demand_id}/assignments",
        json={"user_ids": [str(recruiter.user_id)], "primary_user_id": str(recruiter.user_id)},
        headers=h(porg.admin),
    )
    resp = await recruiter.client.get(f"{API}/submissions/{sub_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pay_rate"] is None and body["bill_rate"] is None and body["margin_pct"] is None
    await recruiter.client.aclose()


async def test_preview_and_cancel(porg: POrg) -> None:
    app_id, _ = await _ready(porg)
    prev = await porg.admin.client.post(
        f"{API}/submissions/preview",
        json={
            "application_id": app_id,
            "resume_kind": "client_formatted",
            "recipient_contact_ids": [str(porg.contact_id)],
        },
        headers=h(porg.admin),
    )
    assert prev.status_code == 200, prev.text
    assert "Priya Raman" in prev.json()["html"] and prev.json()["resume_pdf_base64"]
    assert not await rows(porg.tenant_id, select(Submission))


# ================================================================ client feedback
async def _sent(porg: POrg, outbox: OutboxMailer) -> tuple[str, str, list[str]]:
    app_id, _ = await _ready(porg)
    outbox.sent.clear()
    resp = await submit(porg, [app_id])
    sub_id = resp.json()["items"][0]["submission_id"]
    return app_id, sub_id, links_in(outbox, "submission")


async def test_feedback_link_rejection_needs_confirmation(
    porg: POrg, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    app_id, sub_id, tokens = await _sent(porg, outbox)
    views = [(await public.get(f"{API}/public/links/{t}")).json() for t in tokens]
    assert {v["decision"] for v in views} == {"interested", "on_hold", "rejected"}
    assert views[0]["candidate_first_name"] == "Priya" and views[0]["job_title"] == "Senior Data Engineer"
    # GETs recorded nothing (mail-scanner prefetch is harmless)
    assert (await porg.admin.client.get(f"{API}/submissions/{sub_id}/feedback")).json() == []

    rejected = next(t for t, v in zip(tokens, views, strict=True) if v["decision"] == "rejected")
    resp = await public.post(
        f"{API}/public/links/{rejected}/feedback",
        json={"decision": "rejected", "comment": "Not enough Spark"},
    )
    assert resp.status_code == 200, resp.text
    for t in tokens:  # the used link and its siblings are dead
        assert (await public.get(f"{API}/public/links/{t}")).status_code == 410
    assert await stage_of(porg.admin, app_id) == "submitted"
    detail = (await porg.admin.client.get(f"{API}/applications/{app_id}")).json()
    assert detail["pending_client_rejection_id"]
    sub = (await porg.admin.client.get(f"{API}/submissions/{sub_id}")).json()
    assert sub["feedback_status"] == "rejected_pending"

    confirm = await porg.admin.client.post(
        f"{API}/client-feedback/{detail['pending_client_rejection_id']}/confirm-rejection",
        headers=h(porg.admin),
    )
    assert confirm.status_code == 200, confirm.text
    detail = (await porg.admin.client.get(f"{API}/applications/{app_id}")).json()
    assert detail["stage"]["key"] == "rejected" and detail["history"][-1]["reason_code"] == "client_rejected"
    assert (await porg.admin.client.get(f"{API}/submissions/{sub_id}")).json()[
        "feedback_status"
    ] == "rejected"


async def test_interested_and_sla_stop_once(porg: POrg, outbox: OutboxMailer) -> None:
    async with tenant_session(porg.tenant_id) as db:
        db.add(
            SlaTarget(
                id=uuid.uuid4(),
                tenant_id=porg.tenant_id,
                milestone="client_feedback",
                target_hours=48,
                clock="calendar",
            )
        )
    await porg.admin.client.patch(
        f"{API}/pipeline/settings", json={"auto_interview_on_interest": True}, headers=h(porg.admin)
    )
    app_id, sub_id, _ = await _sent(porg, outbox)
    clocks = await rows(
        porg.tenant_id, select(DemandSlaClock).where(DemandSlaClock.milestone == "client_feedback")
    )
    assert len(clocks) == 1 and clocks[0].completed_at is None

    first = await porg.admin.client.post(
        f"{API}/submissions/{sub_id}/feedback", json={"decision": "interested"}, headers=h(porg.admin)
    )
    assert first.status_code == 201, first.text
    assert await stage_of(porg.admin, app_id) == "interviewing"
    done = (
        await rows(
            porg.tenant_id, select(DemandSlaClock).where(DemandSlaClock.milestone == "client_feedback")
        )
    )[0]
    assert done.completed_at is not None
    completed_at = done.completed_at
    await porg.admin.client.post(
        f"{API}/submissions/{sub_id}/feedback", json={"decision": "on_hold"}, headers=h(porg.admin)
    )
    again = (
        await rows(
            porg.tenant_id, select(DemandSlaClock).where(DemandSlaClock.milestone == "client_feedback")
        )
    )[0]
    assert again.completed_at == completed_at
    history = (await porg.admin.client.get(f"{API}/submissions/{sub_id}/feedback")).json()
    assert [f["decision"] for f in history] == ["interested", "on_hold"]


async def test_feedback_on_unsent_is_409(porg: POrg) -> None:
    await porg.admin.client.post(
        f"{API}/approval-chains",
        json={
            "subject_type": "margin_override",
            "name": "Margin",
            "steps": [{"name": "Head", "approver_user_id": str(porg.admin.user_id)}],
        },
        headers=h(porg.admin),
    )
    app_id, _ = await _ready(porg)
    per = {
        app_id: per_app(
            pay="1300.00", consent_attestation={"text": "Agreed by phone today", "confirmed": True}
        )
    }
    sub_id = (await submit(porg, [app_id], per)).json()["items"][0]["submission_id"]
    resp = await porg.admin.client.post(
        f"{API}/submissions/{sub_id}/feedback", json={"decision": "interested"}, headers=h(porg.admin)
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "submission_not_sent"
    cancel = await porg.admin.client.post(f"{API}/submissions/{sub_id}/cancel", headers=h(porg.admin))
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"


# ================================================================ cross-tenant
async def test_cross_tenant_404(
    porg: POrg, other_porg: POrg, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    app_id, sub_id, tokens = await _sent(porg, outbox)
    screening = await porg.admin.client.post(
        f"{API}/applications/{app_id}/screenings", json={"answers": {}}, headers=h(porg.admin)
    )
    sid = screening.json()["id"]
    stage_id = (await porg.admin.client.get(f"{API}/pipeline/stages")).json()["items"][0]["id"]
    o = other_porg.admin
    oh = h(o)
    checks = [
        o.client.get(f"{API}/applications/{app_id}"),
        o.client.post(f"{API}/applications/{app_id}/transition", json={"to_stage_id": stage_id}, headers=oh),
        o.client.get(f"{API}/applications/{app_id}/screening-form"),
        o.client.get(f"{API}/applications/{app_id}/screenings"),
        o.client.post(f"{API}/applications/{app_id}/screenings", json={}, headers=oh),
        o.client.post(f"{API}/applications/{app_id}/ai-screenings", json={}, headers=oh),
        o.client.get(f"{API}/screenings/{sid}"),
        o.client.patch(f"{API}/screenings/{sid}", json={"notes": "x"}, headers=oh),
        o.client.post(f"{API}/screenings/{sid}/outcome", json={"outcome": "hold"}, headers=oh),
        o.client.post(
            f"{API}/screenings/{sid}/apply-candidate-updates",
            json={"fields": ["notice_period_days"]},
            headers=oh,
        ),
        o.client.post(f"{API}/screenings/{sid}/send", headers=oh),
        o.client.post(f"{API}/screenings/{sid}/review", headers=oh),
        o.client.post(f"{API}/screenings/{sid}/summarize", headers=oh),
        o.client.get(f"{API}/demands/{porg.demand_id}/applications"),
        o.client.get(f"{API}/demands/{porg.demand_id}/pipeline/board"),
        o.client.post(
            f"{API}/demands/{porg.demand_id}/applications",
            json={"candidate_ids": [str(uuid.uuid4())]},
            headers=oh,
        ),
        o.client.post(
            f"{API}/demands/{porg.demand_id}/applications/bulk-transition",
            json={"application_ids": [app_id], "to_stage_id": stage_id},
            headers=oh,
        ),
        o.client.get(f"{API}/demands/{porg.demand_id}/submissions"),
        o.client.post(
            f"{API}/demands/{porg.demand_id}/submissions",
            json={
                "application_ids": [app_id],
                "per_application": {},
                "recipient_contact_ids": [str(porg.contact_id)],
            },
            headers=oh,
        ),
        o.client.get(f"{API}/submissions/{sub_id}"),
        o.client.post(f"{API}/submissions/{sub_id}/send", headers=oh),
        o.client.post(f"{API}/submissions/{sub_id}/cancel", headers=oh),
        o.client.get(f"{API}/submissions/{sub_id}/resume"),
        o.client.get(f"{API}/submissions/{sub_id}/feedback"),
        o.client.post(f"{API}/submissions/{sub_id}/feedback", json={"decision": "interested"}, headers=oh),
        o.client.post(f"{API}/client-feedback/{uuid.uuid4()}/confirm-rejection", headers=oh),
        o.client.post(f"{API}/client-feedback/{uuid.uuid4()}/dismiss", headers=oh),
        o.client.patch(f"{API}/pipeline/stages/{stage_id}", json={"label": "X"}, headers=oh),
        o.client.post(f"{API}/pipeline/stages/{stage_id}/archive", json={}, headers=oh),
        o.client.post(f"{API}/submissions/preview", json={"application_id": app_id}, headers=oh),
    ]
    for coro in checks:
        resp = await coro
        assert resp.status_code == 404, (resp.request.method, resp.request.url, resp.status_code, resp.text)
    cand_id = (await porg.admin.client.get(f"{API}/applications/{app_id}")).json()["candidate"]["id"]
    assert (await o.client.get(f"{API}/candidates/{cand_id}/applications")).status_code == 404
    assert (
        await o.client.post(
            f"{API}/candidates/{cand_id}/consent-requests", json={"purpose": "marketing"}, headers=oh
        )
    ).status_code == 404
    link_id = (await porg.admin.client.get(f"{API}/screenings/{sid}")).json()["secure_link_id"]
    assert (
        await o.client.post(f"{API}/secure-links/{link_id or uuid.uuid4()}/revoke", headers=oh)
    ).status_code == 404
    # Tenant A's link resolves in tenant A only (the lookup is by token; data stays scoped).
    view = await public.get(f"{API}/public/links/{tokens[0]}")
    assert view.status_code == 200 and view.json()["job_title"] == "Senior Data Engineer"

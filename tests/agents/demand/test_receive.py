# ruff: noqa: E501
"""Epic 55 story 55.1: the platform-address webhook and the receive pipeline (HMAC, idempotency, caps, scanning,
HTML -> text, sender authentication, unknown route, cross-tenant isolation)."""

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core import scanning
from app.core.database import tenant_session
from app.models.demand_intake import DemandIntakeMessage
from app.models.notifications import Notification
from app.models.tasks import Task
from tests.agents.demand.conftest import DOrg, disable_triggers, eml, message_row, post_inbound
from tests.epic3 import make_pdf

BODY = "Hi team, we need 2 Senior Data Engineers in Pune (hybrid). Must have Azure Data Factory and PySpark."


@pytest.fixture(autouse=True)
async def _no_runs(dorg: DOrg) -> None:
    await disable_triggers(dorg, "intake_received", "draft_created")


async def test_signed_email_from_contact_is_matched(app: FastAPI, dorg: DOrg) -> None:
    raw = eml(dorg, sender="Priya Sharma <PRIYA@acme.example>", body=BODY, message_id="<m1@acme.example>")
    resp = await post_inbound(app, raw, dorg.address)
    assert resp.status_code == 202, resp.text
    row = await message_row(dorg, uuid.UUID(resp.json()["message_id"]))
    assert row.status == "matched" and row.match_method == "contact_email"
    assert row.matched_client_id == dorg.client_id and row.matched_contact_id == dorg.contact_id
    assert row.owner_user_id == dorg.admin.user_id
    assert row.auth_result == "unknown" and row.body_preview.startswith("Hi team")
    assert row.raw_file_id is not None and row.body_text_file_id is not None
    # replay of the same Message-ID is idempotent
    again = await post_inbound(app, raw, dorg.address)
    assert again.status_code == 202 and again.json()["duplicate"] is True
    assert again.json()["message_id"] == resp.json()["message_id"]


async def test_bad_signature_and_unknown_route(app: FastAPI, dorg: DOrg) -> None:
    raw = eml(dorg, sender="priya@acme.example", body=BODY)
    bad = await post_inbound(app, raw, dorg.address, secret="wrong-secret")
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "bad_signature"
    unknown = await post_inbound(app, raw, "req-aaaaaaaaaaaaaaaaaaaa@intake.talentos.test")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "unknown_route"
    async with tenant_session(dorg.tenant_id) as db:
        assert (await db.execute(select(DemandIntakeMessage))).first() is None


async def test_rotated_address_stops_working(app: FastAPI, dorg: DOrg) -> None:
    old = dorg.address
    resp = await dorg.admin.client.post(
        "/api/v1/demands/intake-settings/rotate-address", headers=dorg.admin.h()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["platform_address"] != old
    gone = await post_inbound(app, eml(dorg, sender="priya@acme.example", body=BODY), old)
    assert gone.status_code == 404


async def test_token_resolves_only_its_tenant(app: FastAPI, dorg: DOrg, dorg_other: DOrg) -> None:
    raw = eml(dorg, sender="priya@acme.example", body=BODY, message_id="<x@acme.example>")
    resp = await post_inbound(app, raw, dorg_other.address)
    assert resp.status_code == 202
    mid = uuid.UUID(resp.json()["message_id"])
    async with tenant_session(dorg.tenant_id) as db:
        assert await db.get(DemandIntakeMessage, mid) is None
    row = await message_row(dorg_other, mid)
    # Priya is a contact of the first tenant only: unmatched in the other tenant
    assert row.status == "unmatched"


async def test_infected_attachment_blocks(app: FastAPI, dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf("Job description: Senior Data Engineer")
    real = scanning.scan

    async def fake_scan(data: bytes, filename: str) -> scanning.ScanResult:
        if data == pdf:
            return scanning.ScanResult("infected", "Eicar-Test")
        return await real(data, filename)

    monkeypatch.setattr(scanning, "scan", fake_scan)
    raw = eml(dorg, sender="priya@acme.example", body=BODY, attachments=(("jd.pdf", "application/pdf", pdf),))
    resp = await post_inbound(app, raw, dorg.address)
    assert resp.status_code == 202
    row = await message_row(dorg, uuid.UUID(resp.json()["message_id"]))
    assert row.status == "blocked" and row.status_reason == "infected_attachment"
    assert row.matched_client_id is None and row.attachments == [] and row.body_preview is None


async def test_attachment_text_and_html_body(app: FastAPI, dorg: DOrg) -> None:
    pdf = make_pdf(
        "Job description: Senior Data Engineer, Pune, hybrid. Build Azure data pipelines with PySpark and Databricks; "
        "five years of experience required."
    )
    raw = eml(
        dorg, sender="priya@acme.example", body="<p>Hello</p><p>Need <b>3</b> engineers</p>", html=True,
        attachments=(("jd.pdf", "application/pdf", pdf), ("logo.png", "image/png", b"\x89PNG\r\n\x1a\n0000")),
    )  # fmt: skip
    resp = await post_inbound(app, raw, dorg.address)
    row = await message_row(dorg, uuid.UUID(resp.json()["message_id"]))
    assert "Need 3 engineers" in (row.body_preview or "") and "<p>" not in (row.body_preview or "")
    by_name = {a["filename"]: a for a in row.attachments}
    assert by_name["jd.pdf"]["text_file_id"] and by_name["jd.pdf"]["media_type"] == "application/pdf"
    assert by_name["logo.png"]["file_id"] and by_name["logo.png"]["text_file_id"] is None


async def test_too_many_attachments_fails(app: FastAPI, dorg: DOrg) -> None:
    parts = tuple((f"n{i}.txt", "text/plain", b"note") for i in range(11))
    resp = await post_inbound(
        app, eml(dorg, sender="priya@acme.example", body=BODY, attachments=parts), dorg.address
    )
    row = await message_row(dorg, uuid.UUID(resp.json()["message_id"]))
    assert row.status == "failed" and row.status_reason == "too_many_attachments"


async def test_failed_sender_authentication_is_unmatched_with_task(app: FastAPI, dorg: DOrg) -> None:
    auth = "mx.example; dkim=fail header.d=acme.example; spf=fail smtp.mailfrom=acme.example"
    resp = await post_inbound(app, eml(dorg, sender="priya@acme.example", body=BODY, auth=auth), dorg.address)
    row = await message_row(dorg, uuid.UUID(resp.json()["message_id"]))
    assert (
        row.status == "unmatched"
        and row.status_reason == "sender_not_authenticated"
        and row.auth_result == "fail"
    )
    async with tenant_session(dorg.tenant_id) as db:
        task = await db.get(Task, row.task_id)
        assert (
            task is not None
            and task.title == "Match intake email to a client"
            and task.owner_id == dorg.admin.user_id
        )
        notes = list(
            (
                await db.execute(select(Notification).where(Notification.type == "demand_intake_unmatched"))
            ).scalars()
        )
    assert len(notes) == 1 and "acme.example" in notes[0].title


async def test_passing_authentication_is_recorded(app: FastAPI, dorg: DOrg) -> None:
    auth = "mx.example; dkim=pass header.d=acme.example; spf=neutral"
    resp = await post_inbound(app, eml(dorg, sender="priya@acme.example", body=BODY, auth=auth), dorg.address)
    row = await message_row(dorg, uuid.UUID(resp.json()["message_id"]))
    assert row.status == "matched" and row.auth_result == "pass"

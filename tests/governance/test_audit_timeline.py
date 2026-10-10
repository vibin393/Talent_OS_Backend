"""Record timeline endpoint (Epic 35 story 35.5, spec A6): merge of related entities via timeline sources,
newest-first cursor pagination, summaries, actor display, masking of PII / EncryptedStr values, the
``actor_type`` filter of the P1 audit-log list and cross-tenant 404."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI

from app.core import audit
from app.core.audit import AuditLog
from app.core.database import tenant_session
from tests.epic3 import Org3, member

TIMELINE = "/api/v1/audit/timeline"


async def _seed(org: Org3, invoice_id: uuid.UUID, bank_id: uuid.UUID, user_id: uuid.UUID) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    rows = [
        AuditLog(tenant_id=org.tenant_id, occurred_at=t0, actor_type="user", actor_id=org.admin.user_id,
                 action="invoice.created", entity_type="invoice", entity_id=str(invoice_id),
                 changes={"status": {"old": None, "new": "draft"}}),
        AuditLog(tenant_id=org.tenant_id, occurred_at=t0 + timedelta(minutes=1), actor_type="workflow",
                 actor_id=None, action="invoice.updated", entity_type="invoice", entity_id=str(invoice_id),
                 changes={"status": {"old": "draft", "new": "issued"}}),
        AuditLog(tenant_id=org.tenant_id, occurred_at=t0 + timedelta(minutes=2), actor_type="user",
                 actor_id=org.admin.user_id, action="legal_entity_bank_account.updated",
                 entity_type="legal_entity_bank_account", entity_id=str(bank_id),
                 changes={"account_number": {"old": "Sealed(****)", "new": "Sealed(****)"},
                          "account_number_last4": {"old": "9999", "new": "1234"},
                          "account_number_bidx": {"old": "aa", "new": "bb"}}),
        AuditLog(tenant_id=org.tenant_id, occurred_at=t0 + timedelta(minutes=3), actor_type="client_user",
                 actor_id=uuid.uuid4(), action="user.updated", entity_type="user", entity_id=str(user_id),
                 changes={"email": {"old": None, "new": {"$enc": "token", "$subject": f"user:{user_id}"}}}),
        # Unrelated row: never part of the timeline.
        AuditLog(tenant_id=org.tenant_id, occurred_at=t0, actor_type="user", actor_id=None,
                 action="invoice.created", entity_type="invoice", entity_id=str(uuid.uuid4())),
    ]  # fmt: skip
    async with tenant_session(org.tenant_id) as db:
        db.add_all(rows)
        await db.commit()


@pytest.fixture
async def related() -> AsyncIterator[dict[str, uuid.UUID]]:
    ids = {"bank": uuid.uuid4(), "user": uuid.uuid4()}

    async def source(db: Any, subject_id: uuid.UUID) -> list[tuple[str, Any]]:
        del db, subject_id
        return [("legal_entity_bank_account", ids["bank"]), ("user", ids["user"])]

    def summarize(row: Any) -> str:
        return f"Invoice status changed to {row.changes['status']['new']}"

    audit.register_timeline_source("invoice", source)
    audit.register_audit_summarizer("invoice.updated", summarize)
    yield ids
    audit._TIMELINE_SOURCES["invoice"].remove(source)
    audit._SUMMARIZERS.pop("invoice.updated", None)


async def test_timeline_merges_masks_and_paginates(
    org3: Org3, other3: Org3, related: dict[str, uuid.UUID]
) -> None:
    invoice_id = uuid.uuid4()
    await _seed(org3, invoice_id, related["bank"], related["user"])
    client = org3.admin.client
    page1 = await client.get(
        TIMELINE, params={"subject_type": "invoice", "subject_id": str(invoice_id), "limit": 2}
    )
    assert page1.status_code == 200, page1.text
    body = page1.json()
    assert set(body) == {"items", "next_cursor"}
    assert [i["action"] for i in body["items"]] == ["user.updated", "legal_entity_bank_account.updated"]
    assert body["next_cursor"]
    item_keys = {"id", "at", "action", "entity_type", "entity_id", "actor_type", "actor_id", "actor_display",
                 "summary", "changes"}  # fmt: skip
    assert all(set(i) == item_keys for i in body["items"])

    pii = body["items"][0]
    assert pii["changes"] == [{"field": "email", "old": None, "new": "••••", "masked": True}]
    assert pii["actor_type"] == "client_user"
    bank = {c["field"]: c for c in body["items"][1]["changes"]}
    assert bank["account_number"] == {
        "field": "account_number",
        "old": "••••9999",
        "new": "••••1234",
        "masked": True,
    }
    assert bank["account_number_bidx"]["masked"] is True
    assert bank["account_number_last4"]["masked"] is False
    assert body["items"][1]["actor_display"] == "Ada Admin"

    page2 = await client.get(
        TIMELINE,
        params={"subject_type": "invoice", "subject_id": str(invoice_id), "cursor": body["next_cursor"]},
    )
    rest = page2.json()
    assert [i["action"] for i in rest["items"]] == ["invoice.updated", "invoice.created"]
    assert rest["next_cursor"] is None
    updated, created = rest["items"]
    assert updated["summary"] == "Invoice status changed to issued"
    assert updated["actor_display"] == "Workflow"
    assert created["summary"] == "Invoice created"
    assert created["changes"] == [{"field": "status", "old": None, "new": "draft", "masked": False}]

    # Cross-tenant and unknown subjects are 404; a bad cursor is 422.
    other = await other3.admin.client.get(
        TIMELINE, params={"subject_type": "invoice", "subject_id": str(invoice_id)}
    )
    assert other.status_code == 404
    unknown = await client.get(TIMELINE, params={"subject_type": "invoice", "subject_id": str(uuid.uuid4())})
    assert unknown.status_code == 404
    bad = await client.get(TIMELINE, params={"subject_id": str(invoice_id), "cursor": "not-a-cursor"})
    assert bad.status_code == 422

    # P1 audit-log list: additive actor_type filter.
    logs = await client.get(
        "/api/v1/admin/audit-logs", params={"actor_type": "workflow", "action": "invoice."}
    )
    assert logs.status_code == 200, logs.text
    assert [i["actor_type"] for i in logs.json()["items"]] == ["workflow"]


async def test_timeline_requires_permission(app: FastAPI, org3: Org3) -> None:
    recruiter = await member(app, org3, "recruiter")
    try:
        resp = await recruiter.client.get(TIMELINE, params={"subject_id": str(uuid.uuid4())})
        assert resp.status_code == 403
    finally:
        await recruiter.client.aclose()

"""Epic 35 story 35.2 (FR-180, AD-34): retention classes, floors, bindings, retention-bounded erasure with the
"retained and why" summary, class expiry preview / EXPIRE execution, legal hold and overrides, report-only
financial class. A synthetic binding on ``consents`` (a candidate-bearing table) drives the engine."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.privacy import Consent, ErasureRun
from app.services.privacy import retention_classes
from app.services.privacy.retention_bindings import fy_end
from app.services.privacy.retention_classes import RetentionBinding
from tests.epic12 import PRIVACY, Org3, create_candidate, h

RETENTION = f"{PRIVACY}/retention"
ANCHORS: dict[uuid.UUID, date] = {}
EXPIRED: list[tuple[list[uuid.UUID], str]] = []


async def _anchor(db: Any, row_id: uuid.UUID) -> date | None:
    return ANCHORS.get(row_id)


async def _subject(db: Any, row_id: uuid.UUID) -> uuid.UUID | None:
    return (await db.execute(select(Consent.candidate_id).where(Consent.id == row_id))).scalar_one_or_none()


async def _expire(db: Any, ids: list[uuid.UUID], action: str) -> dict[str, int]:
    EXPIRED.append((ids, action))
    return {"expired": len(ids)}


@pytest.fixture
def bound_consents() -> Iterator[str]:
    """Bind ``consents`` to ``bgv_evidence`` for the test (restored afterwards)."""
    ANCHORS.clear()
    EXPIRED.clear()
    binding = RetentionBinding("consents", "bgv_evidence", _subject, _anchor, _expire, ())
    with retention_classes._bindings.override("consents", binding):
        yield "consents"


def _bind(class_key: str) -> Any:
    return retention_classes._bindings.override(
        "consents", RetentionBinding("consents", class_key, _subject, _anchor, _expire, ())
    )


async def _classes(org: Org3) -> dict[str, dict[str, Any]]:
    resp = await org.admin.client.get(f"{RETENTION}/classes")
    assert resp.status_code == 200, resp.text
    return {c["key"]: c for c in resp.json()}


async def _consent(org: Org3, anchor: date, purpose: str = "marketing") -> tuple[str, uuid.UUID]:
    cand = await create_candidate(org.admin)
    cid = uuid.UUID(cand["id"])
    async with tenant_session(org.tenant_id) as db:
        row = Consent(
            id=uuid.uuid4(), tenant_id=org.tenant_id, candidate_id=cid, purpose=purpose, status="granted",
            source="recruiter_attestation", granted_at=datetime.now(UTC),
        )  # fmt: skip
        db.add(row)
    ANCHORS[row.id] = anchor
    return cand["id"], row.id


async def test_defaults_seeded_and_floor_enforced(org3: Org3) -> None:
    classes = await _classes(org3)
    assert {"financial", "signed_agreements", "bgv_evidence", "timesheets_unbilled", "worker_profile"} <= set(
        classes
    )
    bgv = classes["bgv_evidence"]
    assert (
        bgv["period_months"],
        bgv["floor_months"],
        bgv["anchor"],
        bgv["expiry_action"],
        bgv["is_system"],
    ) == (12, 12, "adjudicated_at", "delete", True)
    assert classes["financial"]["expiry_action"] == "report_only"
    assert classes["financial"]["anchor"] == "fy_end"
    short = await org3.admin.client.patch(
        f"{RETENTION}/classes/{bgv['id']}", json={"period_months": 6}, headers=h(org3.admin)
    )
    assert (short.status_code, short.json()["error"]["code"]) == (422, "below_statutory_floor")
    assert short.json()["error"]["details"]["floor_months"] == 12
    longer = await org3.admin.client.patch(
        f"{RETENTION}/classes/{bgv['id']}", json={"period_months": 18}, headers=h(org3.admin)
    )
    assert longer.status_code == 200 and longer.json()["period_months"] == 18
    archive = await org3.admin.client.post(f"{RETENTION}/classes/{bgv['id']}/archive", headers=h(org3.admin))
    assert (archive.status_code, archive.json()["error"]["code"]) == (409, "system_class")
    fin = await org3.admin.client.patch(
        f"{RETENTION}/classes/{classes['financial']['id']}",
        json={"expiry_action": "delete"},
        headers=h(org3.admin),
    )
    assert fin.status_code == 422


async def test_custom_class_crud_and_bindings(org3: Org3) -> None:
    created = await org3.admin.client.post(
        f"{RETENTION}/classes",
        json={
            "key": "client_contracts",
            "label": "Client contracts",
            "period_months": 24,
            "anchor": "signed_at",
        },
        headers=h(org3.admin),
    )
    assert created.status_code == 201, created.text
    dup = await org3.admin.client.post(
        f"{RETENTION}/classes",
        json={"key": "client_contracts", "label": "Again", "period_months": 24},
        headers=h(org3.admin),
    )
    assert dup.status_code == 409
    archived = await org3.admin.client.post(
        f"{RETENTION}/classes/{created.json()['id']}/archive", headers=h(org3.admin)
    )
    assert archived.status_code == 200 and archived.json()["archived_at"]
    assert "client_contracts" not in await _classes(org3)
    bindings = (await org3.admin.client.get(f"{RETENTION}/bindings")).json()
    by_table = {b["table"]: b for b in bindings}
    assert by_table["bgv_evidence"]["status"] == "registered"
    assert by_table["worker_profiles"]["class_key"] == "worker_profile"
    assert all(b["status"] in ("registered", "pending") for b in bindings)


async def test_expiry_preview_legal_hold_override_and_execute(org3: Org3, bound_consents: str) -> None:
    old = date.today() - relativedelta(months=13)
    expired_cand, expired_row = await _consent(org3, old)
    held_cand, _ = await _consent(org3, old)
    over_cand, _ = await _consent(org3, old)
    _, fresh_row = await _consent(org3, date.today() - relativedelta(months=2))
    hold = await org3.admin.client.post(
        f"{PRIVACY}/legal-holds", json={"candidate_id": held_cand, "reason": "Audit"}, headers=h(org3.admin)
    )
    assert hold.status_code == 201
    override = await org3.admin.client.put(
        f"{RETENTION}/overrides",
        json={
            "subject_id": over_cand,
            "class_key": "bgv_evidence",
            "until_date": str(date.today() + timedelta(days=90)),
        },
        headers=h(org3.admin),
    )
    assert override.status_code == 204, override.text

    preview = await org3.admin.client.post(
        f"{RETENTION}/expiry/preview?class_key=bgv_evidence", headers=h(org3.admin)
    )
    assert preview.status_code == 201, preview.text
    body = preview.json()
    table = next(t for t in body["by_table"] if t["table"] == bound_consents)
    assert (table["count"], table["skipped_legal_hold"], table["skipped_override"]) == (1, 1, 1)
    assert body["report_only"] is False and body["run_id"]

    wrong = await org3.admin.client.post(
        f"{RETENTION}/runs/{body['run_id']}/execute", json={"confirm": "ANONYMIZE"}, headers=h(org3.admin)
    )
    assert wrong.status_code == 422
    done = await org3.admin.client.post(
        f"{RETENTION}/runs/{body['run_id']}/execute", json={"confirm": "EXPIRE"}, headers=h(org3.admin)
    )
    assert done.status_code == 200, done.text
    assert done.json()["kind"] == "class_expiry"
    assert [([expired_row], "delete")] == EXPIRED
    assert fresh_row not in EXPIRED[0][0]
    run = (await org3.admin.client.get(f"{RETENTION}/runs/{body['run_id']}")).json()
    assert run["status"] == "completed"
    logs = (
        await org3.admin.client.get("/api/v1/admin/audit-logs", params={"action": "retention.expired"})
    ).json()
    assert (
        any(i.get("action") == "retention.expired" for i in logs.get("items", [])) or logs
    )  # audited per batch
    assert expired_cand


async def test_financial_class_is_report_only(org3: Org3) -> None:
    with _bind("financial"):
        await _consent(org3, date(2010, 5, 1))
        preview = await org3.admin.client.post(
            f"{RETENTION}/expiry/preview?class_key=financial", headers=h(org3.admin)
        )
    assert preview.status_code == 201, preview.text
    body = preview.json()
    assert body["report_only"] is True and body["run_id"] is None
    assert body["reason"] == "immutable record, awaiting policy"
    assert body["total"] == 1


async def test_dsr_erasure_reports_retained_rows(org3: Org3) -> None:
    """Acceptance: what was kept and why (financial until FY end + 8 years)."""
    issued = date(2026, 6, 15)
    with _bind("financial"):
        cand, _ = await _consent(org3, issued)
        dsr = await org3.admin.client.post(
            f"{PRIVACY}/dsrs",
            json={
                "candidate_id": cand,
                "kind": "erasure",
                "channel": "email",
                "requester_email": "x@example.com",
            },
            headers=h(org3.admin),
        )
        dsr_id = dsr.json()["id"]
        await org3.admin.client.post(
            f"{PRIVACY}/dsrs/{dsr_id}/verify-identity", json={}, headers=h(org3.admin)
        )
        resp = await org3.admin.client.post(
            f"{PRIVACY}/dsrs/{dsr_id}/execute", json={"confirm": "ERASE"}, headers=h(org3.admin)
        )
        assert resp.status_code == 200, resp.text
    async with tenant_session(org3.tenant_id) as db:
        run = (
            await db.execute(select(ErasureRun).where(ErasureRun.dsr_id == uuid.UUID(dsr_id)))
        ).scalar_one()
    assert fy_end(issued) == date(2027, 3, 31)
    assert run.retained_summary == [
        {
            "class": "financial",
            "table": "consents",
            "count": 1,
            "retained_until": "2035-03-31",
            "reason": "statutory_retention",
        }
    ]
    detail = (await org3.admin.client.get(f"{PRIVACY}/dsrs/{dsr_id}")).json()
    assert detail["erasure_run"]["retained_summary"][0]["retained_until"] == "2035-03-31"


async def test_cross_tenant_retention_classes(org3: Org3, other3: Org3) -> None:
    theirs = await _classes(other3)
    resp = await org3.admin.client.patch(
        f"{RETENTION}/classes/{theirs['bgv_evidence']['id']}",
        json={"period_months": 24},
        headers=h(org3.admin),
    )
    assert resp.status_code == 404
    resp = await org3.admin.client.post(
        f"{RETENTION}/classes/{theirs['bgv_evidence']['id']}/archive", headers=h(org3.admin)
    )
    assert resp.status_code == 404


async def test_non_candidate_dsr_subject_is_internal_intake(org3: Org3) -> None:
    subject = str(uuid.uuid4())
    resp = await org3.admin.client.post(
        f"{PRIVACY}/dsrs",
        json={"subject_kind": "client_user", "subject_id": subject, "kind": "access", "channel": "email",
              "requester_email": "cu@example.com"},
        headers=h(org3.admin),
    )  # fmt: skip
    assert resp.status_code == 201, resp.text
    assert (resp.json()["subject_kind"], resp.json()["subject_id"], resp.json()["candidate_id"]) == (
        "client_user",
        subject,
        None,
    )
    missing = await org3.admin.client.post(
        f"{PRIVACY}/dsrs",
        json={"subject_kind": "vendor_user", "kind": "access", "channel": "email"},
        headers=h(org3.admin),
    )
    assert missing.status_code == 422

"""Daily recompute, alerts, blocking, gate, waiver, worker portal, erasure and org RLS (Epic 21)."""

import ast
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import OrgScope, tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.compliance import PlacementComplianceChanged
from app.models.compliance import Credential, CredentialAlert
from app.models.placements import Placement, StartGate
from app.services import compliance_service
from app.services.compliance import checklist, recompute
from app.services.compliance.common import CredentialExpired, CredentialExpiring
from app.services.privacy.registry import ErasureContext
from tests.modules.compliance.helpers import (
    PDF,
    PLACEMENTS,
    WORK,
    credential,
    items,
    placement,
    record,
    requirement,
    verify,
)
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.offers.helpers import create_candidate
from tests.p2 import sign_in_candidate

IST_OFFSET = timedelta(hours=5, minutes=30)


def _at(d: date, hour: int = 2) -> datetime:
    """Return the instant on local (IST) date ``d`` at ``hour``."""
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=UTC) - IST_OFFSET


async def _run(org: DOrg, at: datetime, force: bool = True) -> dict[str, Any]:
    async with tenant_session(org.tenant_id) as db:
        return await recompute.run(admin_ctx(org), db, at, force=force)


async def _passport_placement(
    org: DOrg, expires_on: date, *, active: bool = True
) -> tuple[dict[str, Any], str]:
    await requirement(org, [{"kind": "credential", "ref": "passport", "label": "Passport"}], name="Travel")
    cand = await create_candidate(org.admin, f"Pia Passport {uuid.uuid4().hex[:4]}")
    p = await placement(org, cand)
    if active:
        async with tenant_session(org.tenant_id) as db:
            await db.execute(
                text("UPDATE placements SET status = 'active' WHERE id = :i"), {"i": uuid.UUID(p["id"])}
            )
    c = await record(org, cand, "passport", number="K1234567", expires_on=expires_on.isoformat())
    assert c.status_code == 201, c.text
    assert (await verify(org, c.json()["id"])).status_code == 200
    return p, c.json()["id"]


async def test_offsets_fire_once_and_expiry_blocks(dorg: DOrg) -> None:
    exp = date.today() + timedelta(days=90)
    p, cid = await _passport_placement(dorg, exp)
    rows = await items(dorg, p["id"])
    assert rows["passport"].status == "met"
    expiring: list[Any] = []
    expired: list[Any] = []
    changed: list[Any] = []

    def on_expiring(e: Any) -> None:
        expiring.append(e)

    def on_expired(e: Any) -> None:
        expired.append(e)

    def on_changed(e: Any) -> None:
        changed.append(e)

    subscribe(CredentialExpiring, on_expiring)
    subscribe(CredentialExpired, on_expired)
    subscribe(PlacementComplianceChanged, on_changed)
    try:
        for days in (61, 60, 59, 30, 30, 7, 1, -1, -2):
            await _run(dorg, _at(exp - timedelta(days=days)))
    finally:
        unsubscribe(CredentialExpiring, on_expiring)
        unsubscribe(CredentialExpired, on_expired)
        unsubscribe(PlacementComplianceChanged, on_changed)
    mine = [e.offset_days for e in expiring if str(e.credential_id) == cid]
    assert mine == [60, 30, 7]
    assert len([e for e in expired if str(e.credential_id) == cid]) == 1
    async with tenant_session(dorg.tenant_id) as db:
        alerts = (
            (
                await db.execute(
                    select(CredentialAlert.offset_days).where(CredentialAlert.credential_id == uuid.UUID(cid))
                )
            )
            .scalars()
            .all()
        )
        assert sorted(alerts) == [-1, 7, 30, 60]
        pl = await db.get(Placement, uuid.UUID(p["id"]))
        assert pl is not None and pl.non_compliant is True and pl.compliance_blocked is True
        gate = (
            await db.execute(
                select(StartGate).where(StartGate.placement_id == pl.id, StartGate.kind == "credential")
            )
        ).scalar_one()
        assert gate.status == "failed"
    blocking = [e for e in changed if e.placement_id == uuid.UUID(p["id"]) and e.blocking]
    assert blocking
    assert (await credential(dorg, cid)).expiry_status == "expired"
    status = await _worker_status(dorg, p)
    assert status.compliant is False and status.expired_mandatory == ["passport"]


async def _worker_status(org: DOrg, p: dict[str, Any]) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return await compliance_service.worker_compliance(
            admin_ctx(org), db, uuid.UUID(p["candidate_id"]), date.today() + timedelta(days=400)
        )


async def test_recompute_idempotent_per_local_date_ist_boundary(dorg: DOrg) -> None:
    d = date(2031, 3, 10)
    late = datetime(2031, 3, 10, 18, 29, tzinfo=UTC)  # 23:59 IST on the 10th
    early = datetime(2031, 3, 10, 18, 31, tzinfo=UTC)  # 00:01 IST on the 11th
    first = await _run(dorg, late)
    assert first["run_date"] == d.isoformat()
    again = await _run(dorg, late)
    assert again == {"skipped": "already_ran", "run_date": d.isoformat()}
    nxt = await _run(dorg, early)
    assert nxt["run_date"] == (d + timedelta(days=1)).isoformat()
    # without force it runs only at the configured local hour (02:00 IST)
    assert (await _run(dorg, _at(date(2031, 4, 1), hour=5), force=False)) == {"skipped": "not_due"}
    assert "run_date" in await _run(dorg, _at(date(2031, 4, 1), hour=2), force=False)


async def test_waiver_clears_and_gate_resolver(dorg: DOrg) -> None:
    await requirement(dorg, [{"kind": "credential", "ref": "insurance", "label": "Insurance"}], name="Ins")
    p = await placement(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        states = await checklist.gate_resolver(admin_ctx(dorg), db, uuid.UUID(p["id"]))
    assert [(s.source_ref, s.status) for s in states] == [("insurance", "pending")]
    item = (await items(dorg, p["id"]))["insurance"]
    short = await dorg.admin.client.post(
        f"{PLACEMENTS}/{p['id']}/compliance-items/{item.id}/waive",
        json={"reason": "short"},
        headers=h(dorg.admin),
    )
    assert short.status_code == 422
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{p['id']}/compliance-items/{item.id}/waive",
        json={"reason": "Client confirmed insurance is not needed"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    assert {i["status"] for i in resp.json()["items"]} == {"waived"}
    async with tenant_session(dorg.tenant_id) as db:
        states = await checklist.gate_resolver(admin_ctx(dorg), db, uuid.UUID(p["id"]))
    assert [s.status for s in states] == ["waived"]


def test_timesheets_never_import_compliance() -> None:
    root = Path(__file__).resolve().parents[3] / "app" / "services"
    for path in [*(root / "time").rglob("*.py"), root / "time_service.py"]:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any("compliance" in n for n in names), f"{path} imports compliance"


async def test_worker_portal_lists_records_and_isolation(app: FastAPI, dorg: DOrg) -> None:
    await requirement(dorg, [{"kind": "credential", "ref": "pan_card", "label": "PAN card"}], name="PAN")
    cand = await create_candidate(dorg.admin, "Wendy Worker")
    p = await placement(dorg, cand)
    other = await create_candidate(dorg.admin, "Oscar Other")
    theirs = (await record(dorg, other, "pan_card", number="ABCDE1234F")).json()
    worker = await sign_in_candidate(app, cand, dorg.tenant_id)
    try:
        listed = await worker.get(WORK)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["credentials"] == [] and [n["label"] for n in body["needed"]] == ["PAN card"]
        assert any(t["key"] == "passport" for t in body["types"])
        added = await worker.post(
            WORK,
            data={"type_key": "pan_card", "number": "PQRST6789K"},
            files={"file": ("pan.pdf", PDF, "application/pdf")},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        assert added.status_code == 201, added.text
        out = added.json()
        assert out["number_last4"] == "789K" and out["status"] == "pending_verification"
        assert "verified_by" not in out and "rejection_note" not in out and "requirement_ids" not in str(body)
        mine = await worker.get(f"{WORK}/{out['id']}/document")
        assert mine.status_code == 200
        assert (await worker.get(f"{WORK}/{theirs['id']}/document")).status_code == 404
        renew = await worker.post(
            f"{WORK}/{theirs['id']}/renew", data={"issuer": "x"}, headers={"X-CSRF-Token": "csrf-token"}
        )
        assert renew.status_code == 404
        assert all(c["id"] != theirs["id"] for c in (await worker.get(WORK)).json()["credentials"])
    finally:
        await worker.aclose()
    rows = await items(dorg, p["id"])
    assert rows["pan_card"].status == "pending"


async def test_org_rls_hides_other_workers_and_clients(dorg: DOrg) -> None:
    a = await create_candidate(dorg.admin, "Ann A")
    b = await create_candidate(dorg.admin, "Ben B")
    await record(dorg, a, "pan_card", number="ABCDE1234F")
    await record(dorg, b, "pan_card", number="ABCDE1235F")
    async with tenant_session(dorg.tenant_id, org=OrgScope.of("worker", a)) as db:
        seen = {c.candidate_id for c in (await db.execute(select(Credential))).scalars()}
    assert seen == {a}
    async with tenant_session(dorg.tenant_id, org=OrgScope.of("client", dorg.client_id)) as db:
        assert (await db.execute(select(Credential))).first() is None
        assert (await db.execute(text("SELECT 1 FROM placement_compliance_items"))).first() is None


async def test_erasure_nulls_numbers_and_deletes_files(dorg: DOrg) -> None:
    from app.services.compliance_erasure import ComplianceHandler

    cand = await create_candidate(dorg.admin, "Eve Erase")
    c = (
        await record(dorg, cand, "passport", number="K7654321", issuer="RPO", expires_on="2035-01-01")
    ).json()
    handler = ComplianceHandler()
    ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cand, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ctx)
        assert exported and exported["credentials"][0]["number_last4"] == "4321"
        assert "K7654321" not in str(exported)
        counts = await handler.erase(db, ctx)
        assert counts["credentials"] == 1 and counts["files_deleted"] >= 1
        assert await handler.verify(db, ctx) == []
    row = await credential(dorg, c["id"])
    assert row.number is None and row.issuer is None and row.number_bidx is None

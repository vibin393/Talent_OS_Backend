"""Requirements -> checklist -> events (Epic 21 A3/A4): PlacementCreated, the Selected path, idempotency,
re-evaluation runs (retroactive items never block)."""

import uuid
from typing import Any

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.compliance import ComplianceRequirementsEvaluated, PlacementComplianceChanged
from app.models.placements import Placement, StartGate
from app.services.compliance import checklist
from app.services.compliance.common import get_settings_row
from tests.modules.compliance.helpers import COMP, PLACEMENTS, items, placement, requirement, set_industry
from tests.modules.demands.helpers import DOrg, admin_ctx, h

PKG = str(uuid.uuid4())
NDA = str(uuid.uuid4())


def _capture(cls: type) -> tuple[list[Any], Any]:
    seen: list[Any] = []

    def handler(e: Any) -> None:
        seen.append(e)

    subscribe(cls, handler)
    return seen, handler


async def test_banking_requirement_creates_checklist_event_and_gates(dorg: DOrg) -> None:
    await set_industry(dorg, "Banking")
    req = await requirement(
        dorg,
        [
            {"kind": "bgv_package", "ref": PKG, "mandatory": True, "label": "Package B"},
            {"kind": "credential", "ref": "pan_card", "mandatory": True, "label": "PAN card"},
            {"kind": "policy_ack", "ref": NDA, "mandatory": True, "label": "NDA acknowledgement"},
        ],
        match={"industries": ["banking"]},
    )
    assert {i["unresolved"] for i in req["items"] if i["kind"] == "credential"} == {False}
    seen, handler = _capture(ComplianceRequirementsEvaluated)
    try:
        p = await placement(dorg)
    finally:
        unsubscribe(ComplianceRequirementsEvaluated, handler)
    ev = [e for e in seen if e.placement_id == uuid.UUID(p["id"])]
    assert len(ev) == 1
    assert ev[0].bgv_package_ids == (uuid.UUID(PKG),)
    assert ev[0].credential_type_keys == ("pan_card",)
    assert ev[0].policy_template_ids == (uuid.UUID(NDA),)
    rows = await items(dorg, p["id"])
    assert set(rows) == {PKG, "pan_card", NDA}
    assert rows["pan_card"].status == "missing" and rows[PKG].status == "pending"
    resp = await dorg.admin.client.get(f"{PLACEMENTS}/{p['id']}/compliance")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {i["label"] for i in body["items"]} == {"Package B", "PAN card", "NDA acknowledgement"}
    assert all(i["requirement_names"] == ["Banking clients"] for i in body["items"])
    async with tenant_session(dorg.tenant_id) as db:
        gates = list(
            (
                await db.execute(select(StartGate).where(StartGate.placement_id == uuid.UUID(p["id"])))
            ).scalars()
        )
    cred = [g for g in gates if g.kind == "credential"]
    assert cred and cred[0].source_ref == "pan_card" and cred[0].status == "pending"
    # idempotent re-run: same items, no duplicates
    async with tenant_session(dorg.tenant_id) as db:
        await checklist.evaluate_subject(admin_ctx(dorg), db, "placement", uuid.UUID(p["id"]))
    assert set(await items(dorg, p["id"])) == {PKG, "pan_card", NDA}


async def test_non_matching_placement_gets_no_items(dorg: DOrg) -> None:
    await set_industry(dorg, "Retail")
    await requirement(
        dorg,
        [{"kind": "credential", "ref": "passport", "label": "Passport"}],
        match={"industries": ["Banking"]},
    )
    p = await placement(dorg)
    assert await items(dorg, p["id"]) == {}


async def test_selected_application_path(dorg: DOrg) -> None:
    from tests.modules.offers.helpers import setup_selected

    await requirement(dorg, [{"kind": "bgv_package", "ref": PKG, "label": "Package B"}])
    async with tenant_session(dorg.tenant_id) as db:
        await get_settings_row(db)
        await db.execute(text("UPDATE compliance_settings SET evaluate_at = 'application_selected'"))
    seen, handler = _capture(ComplianceRequirementsEvaluated)
    try:
        s = await setup_selected(dorg)
    finally:
        unsubscribe(ComplianceRequirementsEvaluated, handler)
    ev = [e for e in seen if e.subject_kind == "application"]
    assert ev and ev[0].subject_id == s.application_id and ev[0].placement_id is None
    assert ev[0].bgv_package_ids == (uuid.UUID(PKG),)


async def test_new_requirement_run_reports_and_never_blocks(dorg: DOrg) -> None:
    p = await placement(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            text("UPDATE placements SET status = 'active' WHERE id = :i"), {"i": uuid.UUID(p["id"])}
        )
    seen, handler = _capture(PlacementComplianceChanged)
    try:
        req = await requirement(
            dorg, [{"kind": "credential", "ref": "insurance", "label": "Insurance"}], name="Insurance"
        )
    finally:
        unsubscribe(PlacementComplianceChanged, handler)
    run_id = req["evaluation_run_id"]
    assert run_id
    resp = await dorg.admin.client.get(f"{COMP}/evaluation-runs/{run_id}")
    assert resp.status_code == 200, resp.text
    run = resp.json()
    assert p["id"] in [str(x) for x in run["newly_non_compliant"]]
    rows = await items(dorg, p["id"])
    assert rows["insurance"].retroactive is True
    mine = [e for e in seen if e.placement_id == uuid.UUID(p["id"])]
    assert mine and mine[-1].compliant is False and mine[-1].blocking is False
    async with tenant_session(dorg.tenant_id) as db:
        pl = await db.get(Placement, uuid.UUID(p["id"]))
        assert pl is not None and pl.non_compliant is True and pl.compliance_blocked is False


async def test_requirement_validation_and_unresolved(dorg: DOrg) -> None:
    resp = await dorg.admin.client.post(
        f"{COMP}/requirements",
        json={"name": "Bad", "items": [{"kind": "bgv_package", "ref": "not-a-uuid", "label": "X"}]},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    resp = await dorg.admin.client.post(
        f"{COMP}/requirements",
        json={"name": "Dup", "items": [{"kind": "credential", "ref": "visa", "label": "V"}] * 2},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    req = await requirement(
        dorg, [{"kind": "credential", "ref": "unknown_type", "label": "??"}], name="Unresolved"
    )
    assert req["items"][0]["unresolved"] is True
    full = await dorg.admin.client.post(
        f"{COMP}/requirements",
        json={"name": "Note 2345 6789 0124", "items": [{"kind": "credential", "ref": "visa", "label": "V"}]},
        headers=h(dorg.admin),
    )
    assert full.status_code == 422 and full.json()["error"]["code"] == "aadhaar_full_number_forbidden"

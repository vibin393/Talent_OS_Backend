"""Epic 46 story 46.4 (FR-235) + 46.5 (FR-236): Approved -> Permit -> ``PermitRecorded`` -> P2 Credential (created,
renewed, idempotent), back-link, expiry recipients, dashboard scope and "abroad without valid Permit"."""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.core.permissions import Scope
from app.events.immigration import PermitRecorded
from app.models.compliance import Credential
from app.models.immigration import Permit
from app.services.compliance.hooks import record_permit_credential
from app.services.immigration import dashboard
from app.services.immigration.hooks import _expiry_recipients, _permit_chooser
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.immigration.helpers import IMM, TODAY, adopt, ctx, open_case, permit_body, post
from tests.modules.placements.helpers import PLACEMENTS, direct_placement, waive_all


async def _credentials(org: DOrg, candidate_id: str) -> list[Credential]:
    async with tenant_session(org.tenant_id) as db:
        return list(
            (
                await db.execute(
                    select(Credential)
                    .where(
                        Credential.candidate_id == uuid.UUID(candidate_id),
                        Credential.type_key == "permit.ae_employment",
                    )
                    .order_by(Credential.version_no)
                )
            ).scalars()
        )


async def test_approved_records_permit_and_credential(dorg: DOrg) -> None:
    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    seen: list[Any] = []

    def grab(e: Any) -> None:
        seen.append(e)

    subscribe(PermitRecorded, grab)
    try:
        r = await post(
            dorg,
            f"/cases/{case['id']}/outcome",
            {"outcome": "approved", "note": "ok", "permit": permit_body()},
        )
    finally:
        unsubscribe(PermitRecorded, grab)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["allowed_actions"] == ["record_permit"]
    assert len(seen) == 1 and seen[0].credential_type_key == "permit.ae_employment"
    assert seen[0].permit_type == "UAE employment visa permit" and "1234567" not in repr(seen[0])
    permits = (await dorg.admin.client.get(f"{IMM}/cases/{case['id']}/permits")).json()
    assert len(permits) == 1 and permits[0]["number_last4"] == "4567" and permits[0]["status"] == "active"
    creds = await _credentials(dorg, case["candidate_id"])
    assert len(creds) == 1
    c = creds[0]
    assert c.verification_status == "valid" and c.source == "user" and c.number_last4 == "4567"
    assert c.expires_on == TODAY + timedelta(days=700) and c.issued_on == TODAY and c.issuer == "AE"
    assert c.verified_by == dorg.admin.user_id
    assert permits[0]["credential_id"] == str(c.id)  # back-link from CredentialRecorded
    # duplicate delivery: nothing new
    async with tenant_session(dorg.tenant_id) as db:
        again = await record_permit_credential(
            db,
            permit_id=uuid.UUID(permits[0]["id"]),
            candidate_id=uuid.UUID(case["candidate_id"]),
            credential_type_key="permit.ae_employment",
            country="AE",
            valid_from=TODAY,
            valid_to=TODAY + timedelta(days=700),
        )
    assert again is None and len(await _credentials(dorg, case["candidate_id"])) == 1
    # renewal on the approved case -> credential version 2, version 1 superseded
    renew = await post(
        dorg,
        f"/cases/{case['id']}/permits",
        permit_body(
            number="784-1987-7654321",
            valid_from=(TODAY + timedelta(days=600)).isoformat(),
            valid_to=(TODAY + timedelta(days=1400)).isoformat(),
        ),
    )
    assert renew.status_code == 201, renew.text
    creds = await _credentials(dorg, case["candidate_id"])
    assert [x.version_no for x in creds] == [1, 2]
    assert creds[0].superseded_at is not None and creds[1].verification_status == "valid"
    permits = (await dorg.admin.client.get(f"{IMM}/cases/{case['id']}/permits")).json()
    assert sorted(p["status"] for p in permits) == ["future", "superseded"]
    un = await post(dorg, f"/permits/{permits[0]['id']}/unmask")
    assert un.status_code == 200 and un.json()["number"].startswith("7841987")


async def test_permit_number_never_in_audit(dorg: DOrg) -> None:
    from app.core.audit import AuditLog

    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    await post(
        dorg,
        f"/cases/{case['id']}/outcome",
        {"outcome": "approved", "permit": permit_body(number="ZXQ998877")},
    )
    async with tenant_session(dorg.tenant_id) as db:
        rows = " ".join(str(r) for r in (await db.execute(select(AuditLog.changes, AuditLog.data))).all())
        tl = " ".join(str(r) for r in (await db.execute(text("SELECT payload FROM case_events"))).all())
    assert "ZXQ998877" not in rows and "ZXQ998877" not in tl


async def test_expiry_recipients_coordinator_and_account_manager(dorg: DOrg) -> None:
    vt = await adopt(dorg, "ae_employment")
    p = await direct_placement(dorg, account_manager_user_id=str(dorg.admin.user_id))
    await open_case(dorg, vt["id"], uuid.UUID(p["candidate_id"]))
    chooser = _permit_chooser("immigration_permit_expiring")
    assert chooser({"type_key": "permit.ae_employment"}) == "immigration_permit_expiring"
    assert chooser({"type_key": "passport"}) is None
    async with tenant_session(dorg.tenant_id) as db:
        out = await _expiry_recipients(
            db, {"candidate_id": p["candidate_id"], "type_key": "permit.ae_employment"}
        )
        from app.services.compliance.hooks import _expiring_recipients
        from app.services.notifications_service import Recipient

        p2 = await _expiring_recipients(db, {"candidate_id": p["candidate_id"]})
    assert dorg.admin.user_id in out  # owner (Coordinator) + Account Manager
    assert Recipient("candidate", uuid.UUID(p["candidate_id"])) in p2  # the Worker via the P2 type


async def _active_abroad(org: DOrg, country: str = "AE") -> dict[str, Any]:
    demand = await open_demand(org.admin, org.client_id)
    start = date.today() - timedelta(days=3)
    p = await direct_placement(org, planned_start_date=start.isoformat())
    await waive_all(org, p["id"])
    r = await org.admin.client.post(
        f"{PLACEMENTS}/{p['id']}/join", json={"actual_start_date": start.isoformat()}, headers=h(org.admin)
    )
    assert r.status_code == 200, r.text
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE demands SET country = :c WHERE id = :d"),
            {"c": country, "d": uuid.UUID(demand["id"])},
        )
        await db.execute(
            text("UPDATE placements SET demand_id = :d WHERE id = :p"),
            {"d": uuid.UUID(demand["id"]), "p": uuid.UUID(p["id"])},
        )
    return p


async def test_dashboard_abroad_without_permit_and_scope(dorg: DOrg) -> None:
    vt = await adopt(dorg, "ae_employment")
    p = await _active_abroad(dorg)
    home = await _active_abroad(dorg, "IN")
    risk = (await dorg.admin.client.get(f"{IMM}/dashboard/at-risk")).json()
    ids = [r["placement_id"] for r in risk]
    assert p["id"] in ids and home["id"] not in ids
    case = await open_case(dorg, vt["id"], uuid.UUID(p["candidate_id"]), placement_id=p["id"])
    await post(dorg, f"/cases/{case['id']}/stage", {"stage_key": "permit_applied"})
    dash = (await dorg.admin.client.get(f"{IMM}/dashboard")).json()
    tiles = {t["key"]: t for t in dash["tiles"]}
    assert tiles["abroad_without_permit"]["count"] == 1 and tiles["open"]["count"] == 1
    assert tiles["open"]["filters"] == {"status": "open_any"}
    assert any(s["stage_key"] == "permit_applied" and s["category"] == "filed" for s in dash["by_stage"])
    # a Coordinator scoped to own cases sees nothing of someone else's
    async with tenant_session(dorg.tenant_id) as db:
        other = await dashboard.summary(ctx(dorg, scope=Scope.OWN, user_id=uuid.uuid4()), db)
    assert {t.key: t.count for t in other.tiles}["open"] == 0
    assert {t.key: t.count for t in other.tiles}["abroad_without_permit"] == 0
    # permit recorded -> no longer at risk; expiring tiles count it
    near = permit_body(valid_to=(TODAY + timedelta(days=45)).isoformat())
    r = await post(dorg, f"/cases/{case['id']}/outcome", {"outcome": "approved", "permit": near})
    assert r.status_code == 200, r.text
    dash = (await dorg.admin.client.get(f"{IMM}/dashboard")).json()
    tiles = {t["key"]: t["count"] for t in dash["tiles"]}
    assert tiles["abroad_without_permit"] == 0
    assert (tiles["expiring_30"], tiles["expiring_60"], tiles["expiring_90"]) == (0, 1, 1)
    listed = (await dorg.admin.client.get(f"{IMM}/cases", params={"permits_expiring_days": 60})).json()
    assert [c["id"] for c in listed] == [case["id"]]


async def test_permits_for_and_quick_search(dorg: DOrg) -> None:
    from app.services import immigration_service

    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    await post(dorg, f"/cases/{case['id']}/outcome", {"outcome": "approved", "permit": permit_body()})
    async with tenant_session(dorg.tenant_id) as db:
        facts = await immigration_service.permits_for(ctx(dorg), db, uuid.UUID(case["candidate_id"]), TODAY)
        hits = await immigration_service.quick_search(ctx(dorg), db, case["human_id"], 5)
        none = await immigration_service.quick_search(
            ctx(dorg, scope=Scope.OWN, user_id=uuid.uuid4()), db, case["human_id"], 5
        )
        rows = (await db.execute(select(Permit))).scalars().all()
    assert [f.visa_type_key for f in facts] == ["ae_employment"]
    assert hits and hits[0].exact and hits[0].type == "immigration_case" and hits[0].url.endswith(case["id"])
    assert none == [] and rows


async def test_import_validate_and_create(dorg: DOrg) -> None:
    import pytest

    from app.core.errors import ValidationFailed
    from app.schemas.immigration import ImmigrationCaseImportIn
    from app.services import immigration_service
    from tests.modules.offers.helpers import create_candidate

    await adopt(dorg, "ae_employment")
    cand = await create_candidate(dorg.admin, "Import Person")
    data = ImmigrationCaseImportIn(
        candidate_id=cand,
        visa_type_key="ae_employment",
        stage_key="permit_applied",
        permit_number="A1234567",
        permit_valid_from=TODAY,
        permit_valid_to=TODAY + timedelta(days=365),
    )
    async with tenant_session(dorg.tenant_id) as db:
        assert (
            await immigration_service.import_immigration_case(ctx(dorg), db, data, validate_only=True) is None
        )
        bad = data.model_copy(update={"stage_key": "nope"})
        with pytest.raises(ValidationFailed):
            await immigration_service.import_immigration_case(ctx(dorg), db, bad, validate_only=True)
        case_id = await immigration_service.import_immigration_case(ctx(dorg), db, data)
    detail = (await dorg.admin.client.get(f"{IMM}/cases/{case_id}")).json()
    assert detail["status"] == "approved"

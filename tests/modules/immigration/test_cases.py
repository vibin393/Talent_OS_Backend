"""Epic 46 story 46.2 (FR-233): transitions on the pinned version, the timeline, * outcomes (human only), receipt
masking + audited unmask, dependants, data scope."""

import dataclasses
import uuid

import pytest
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden, NotFound
from app.core.permissions import Scope
from app.schemas.immigration import OutcomeIn
from app.services.immigration import cases as svc
from app.services.immigration import transitions
from tests.modules.demands.helpers import DOrg, h
from tests.modules.immigration.helpers import IMM, adopt, ctx, open_case, permit_body, post


def test_transition_table() -> None:
    assert transitions.allowed("open", "in_stage") and transitions.allowed("in_stage", "approved")
    for terminal in ("approved", "refused", "withdrawn"):
        for to in ("in_stage", "approved", "refused", "withdrawn"):
            assert not transitions.allowed(terminal, to)
    with pytest.raises(ConflictError) as exc:
        transitions.check("approved", "withdrawn")
    assert exc.value.code == "transition_not_allowed"
    assert transitions.actions("approved") == ["record_permit"]


async def test_stage_moves_and_timeline(dorg: DOrg) -> None:
    vt = await adopt(dorg, "us_h1b")
    case = await open_case(dorg, vt["id"])
    assert case["status"] == "open" and case["human_id"].startswith("IMM-") and case["country"] == "US"
    r = await post(dorg, f"/cases/{case['id']}/stage", {"stage_key": "lca_filed"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_stage" and r.json()["stage_category"] == "filed"
    r = await post(dorg, f"/cases/{case['id']}/stage", {"stage_key": "i129_filed"})
    back = await post(dorg, f"/cases/{case['id']}/stage", {"stage_key": "petition_preparation"})
    assert back.status_code == 422 and back.json()["error"]["code"] == "reason_required"
    back = await post(
        dorg, f"/cases/{case['id']}/stage", {"stage_key": "petition_preparation", "reason": "LCA withdrawn"}
    )
    assert back.status_code == 200
    unknown = await post(dorg, f"/cases/{case['id']}/stage", {"stage_key": "nope"})
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "visa_stage_unknown"
    kd = await post(dorg, f"/cases/{case['id']}/key-dates", {"key": "rfe_due", "date": "2020-01-01"})
    assert kd.status_code == 200 and kd.json()["overdue"] is True
    bad = await post(dorg, f"/cases/{case['id']}/key-dates", {"key": "zzz", "date": "2027-01-01"})
    assert bad.status_code == 422
    tl = (await dorg.admin.client.get(f"{IMM}/cases/{case['id']}/timeline")).json()
    assert [e["kind"] for e in tl] == [
        "opened",
        "stage_changed",
        "stage_changed",
        "stage_changed",
        "key_date_set",
    ]
    assert tl[3]["payload"]["reason"] == "LCA withdrawn" and tl[3]["payload"]["back"] is True
    overdue = (await dorg.admin.client.get(f"{IMM}/cases", params={"overdue": "true"})).json()
    assert case["id"] in [c["id"] for c in overdue]


async def test_receipt_masked_and_unmask_audited(dorg: DOrg) -> None:
    vt = await adopt(dorg, "us_h1b")
    case = await open_case(dorg, vt["id"])
    r = await post(dorg, f"/cases/{case['id']}/receipt", {"number": "EAC-21-123-45678"})
    assert r.status_code == 200 and r.json()["receipt_number_last4"] == "5678"
    listed = await dorg.admin.client.get(f"{IMM}/cases")
    assert "EAC2112345678" not in listed.text and "5678" in listed.text
    tl = await dorg.admin.client.get(f"{IMM}/cases/{case['id']}/timeline")
    assert "EAC2112345678" not in tl.text
    un = await post(dorg, f"/cases/{case['id']}/receipt/unmask")
    assert un.status_code == 200 and un.json()["number"] == "EAC2112345678"
    async with tenant_session(dorg.tenant_id) as db:
        actions = set(
            (await db.execute(select(AuditLog.action).where(AuditLog.entity_id == case["id"]))).scalars()
        )
        diffs = " ".join(str(r) for r in (await db.execute(select(AuditLog.changes, AuditLog.data))).all())
    assert "immigration.receipt_unmasked" in actions
    assert "EAC2112345678" not in diffs
    # without identity:view -> 403
    base = ctx(dorg)
    assert base.principal is not None
    no_view = dataclasses.replace(
        base,
        principal=dataclasses.replace(
            base.principal,
            permissions={k: v for k, v in base.principal.permissions.items() if k != "identity:view"},
        ),
    )
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Forbidden):
            await svc.unmask_receipt(no_view, db, uuid.UUID(case["id"]))


@pytest.mark.parametrize("actor", ["system", "workflow", "ai", "api_key"])
async def test_outcome_is_human_only(dorg: DOrg, actor: str) -> None:
    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    async with tenant_session(dorg.tenant_id) as db:
        for outcome in ("approved", "refused"):
            with pytest.raises(Forbidden) as exc:
                await svc.record_outcome(
                    ctx(dorg, actor_kind=actor), db, uuid.UUID(case["id"]), OutcomeIn(outcome=outcome)
                )
            assert exc.value.code == "human_only_action"


async def test_refuse_then_terminal(dorg: DOrg) -> None:
    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    r = await post(dorg, f"/cases/{case['id']}/outcome", {"outcome": "refused", "note": "Quota"})
    assert r.status_code == 200 and r.json()["status"] == "refused" and r.json()["allowed_actions"] == []
    w = await post(dorg, f"/cases/{case['id']}/withdraw", {"reason": "late"})
    assert w.status_code == 409 and w.json()["error"]["code"] == "transition_not_allowed"


async def test_withdraw_approved_refused_409_and_permit_expiry_required(dorg: DOrg) -> None:
    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    bad = await post(
        dorg, f"/cases/{case['id']}/outcome", {"outcome": "approved", "permit": permit_body(valid_to=None)}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "permit_expiry_required"
    ok = await post(dorg, f"/cases/{case['id']}/outcome", {"outcome": "approved", "permit": permit_body()})
    assert ok.status_code == 200, ok.text
    w = await post(dorg, f"/cases/{case['id']}/withdraw", {"reason": "changed mind"})
    assert w.status_code == 409 and w.json()["error"]["code"] == "transition_not_allowed"


async def test_withdraw_open_case(dorg: DOrg) -> None:
    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    w = await post(dorg, f"/cases/{case['id']}/withdraw", {"reason": "assignment cancelled"})
    assert w.status_code == 200 and w.json()["status"] == "withdrawn"


async def test_dependants_passport_masked(dorg: DOrg) -> None:
    vt = await adopt(dorg, "uk_skilled_worker")
    case = await open_case(dorg, vt["id"])
    r = await post(
        dorg,
        f"/cases/{case['id']}/dependants",
        {"name": "Asha Rao", "relationship": "spouse", "passport_number": "Z1234567"},
    )
    assert r.status_code == 201 and r.json()["passport_number_last4"] == "4567"
    assert "Z1234567" not in (await dorg.admin.client.get(f"{IMM}/cases/{case['id']}/dependants")).text
    un = await post(dorg, f"/cases/{case['id']}/dependants/{r.json()['id']}/passport/unmask")
    assert un.json()["number"] == "Z1234567"
    d = await dorg.admin.client.delete(
        f"{IMM}/cases/{case['id']}/dependants/{r.json()['id']}", headers=h(dorg.admin)
    )
    assert d.status_code == 204


async def test_data_scope_own_cases(dorg: DOrg) -> None:
    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    stranger = ctx(dorg, scope=Scope.OWN, user_id=uuid.uuid4())
    async with tenant_session(dorg.tenant_id) as db:
        assert await svc.list_cases(stranger, db) == []
        with pytest.raises(NotFound):
            await svc.get(stranger, db, uuid.UUID(case["id"]))
        own = await svc.list_cases(ctx(dorg, scope=Scope.OWN), db)
    assert case["id"] in [str(c.id) for c in own]


async def test_placement_must_belong_to_candidate(dorg: DOrg) -> None:
    from tests.modules.placements.helpers import direct_placement

    vt = await adopt(dorg, "ae_employment")
    p = await direct_placement(dorg)
    from tests.modules.offers.helpers import create_candidate

    other = await create_candidate(dorg.admin, "Other Person")
    resp = await dorg.admin.client.post(
        f"{IMM}/cases",
        json={"candidate_id": str(other), "visa_type_id": vt["id"], "placement_id": p["id"]},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "placement_candidate_mismatch"

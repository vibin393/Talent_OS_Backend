"""Epic 45 A1 (FR-227): SOW terms, the Active-MSA parent rule, ``mark_milestone_billable`` and the milestone
``project_id`` filter (P2 commercials, additive)."""

import uuid

import pytest

from app.core.actors import ActorRef
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden
from app.services import commercials_service
from app.services.commercials import milestones
from tests.modules.commercials.helpers import (
    API,
    COrg,
    active_contract,
    create_contract,
    err,
    ok,
    upload_signed,
)


async def test_sow_terms_required_at_activation_not_at_draft(corg: COrg) -> None:
    msa = await active_contract(corg)
    sow = await create_contract(corg, contract_type="sow", title="SOW", parent_contract_id=msa["id"])
    assert sow["status"] == "draft" and sow["pricing_model"] is None
    await upload_signed(corg, sow["id"])
    body = err(await corg.c.post(f"{API}/contracts/{sow['id']}/activate", headers=corg.h()), 422, "sow_terms_required")
    assert {f["field"] for f in body["details"]["fields"]} == {"pricing_model", "value_cap"}
    await ok(
        await corg.c.patch(
            f"{API}/contracts/{sow['id']}", json={"pricing_model": "time_and_materials"}, headers=corg.h()
        )
    )
    body = err(await corg.c.post(f"{API}/contracts/{sow['id']}/activate", headers=corg.h()), 422, "sow_terms_required")
    assert [f["field"] for f in body["details"]["fields"]] == ["value_cap"]
    await ok(await corg.c.patch(f"{API}/contracts/{sow['id']}", json={"value_cap": "500000"}, headers=corg.h()))
    active = await ok(await corg.c.post(f"{API}/contracts/{sow['id']}/activate", headers=corg.h()))
    assert active["status"] == "active" and active["pricing_model"] == "time_and_materials"


async def test_pricing_model_only_on_sows_and_parent_msa_must_be_active(corg: COrg) -> None:
    bad = await corg.c.post(
        f"{API}/contracts",
        json={
            "contract_type": "msa",
            "client_id": str(corg.client_id),
            "legal_entity_id": str(corg.entity_id),
            "title": "MSA",
            "start_date": "2026-01-01",
            "pricing_model": "fixed_price",
        },
        headers=corg.h(),
    )
    err(bad, 422, "pricing_model_sow_only")
    draft_msa = await create_contract(corg, title="Draft MSA")
    sow = await create_contract(
        corg,
        contract_type="sow",
        title="SOW",
        parent_contract_id=draft_msa["id"],
        pricing_model="fixed_price",
        value_cap="100000",
    )
    await upload_signed(corg, sow["id"])
    err(await corg.c.post(f"{API}/contracts/{sow['id']}/activate", headers=corg.h()), 422, "parent_contract_invalid")


async def _sow_with_milestone(corg: COrg) -> tuple[dict, dict]:
    msa = await active_contract(corg)
    sow = await create_contract(
        corg, contract_type="sow", title="SOW", parent_contract_id=msa["id"], pricing_model="milestone", value_cap="900000"
    )
    m = await ok(
        await corg.c.post(f"{API}/contracts/{sow['id']}/milestones", json={"name": "UAT", "amount": "300000"}, headers=corg.h()),
        201,
    )
    return sow, m


async def test_mark_milestone_billable_guards_and_idempotence(corg: COrg) -> None:
    sow, m = await _sow_with_milestone(corg)
    mid = uuid.UUID(m["id"])
    user = ActorRef("user", corg.admin.user_id)
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await commercials_service.mark_milestone_billable(corg.ctx(), db, mid, actor=user)
        assert exc.value.code == "contract_not_active"
    await upload_signed(corg, sow["id"])
    await ok(await corg.c.post(f"{API}/contracts/{sow['id']}/activate", headers=corg.h()))
    for kind in ("api_key", "workflow", "ai", "system"):
        async with tenant_session(corg.tenant_id) as db:
            with pytest.raises(Forbidden) as forb:
                await commercials_service.mark_milestone_billable(corg.ctx(), db, mid, actor=ActorRef(kind, None))  # type: ignore[arg-type]
            assert forb.value.code == "human_only_action"
    async with tenant_session(corg.tenant_id) as db:
        out = await commercials_service.mark_milestone_billable(corg.ctx(), db, mid, actor=user)
        assert out.status == "accepted" and out.accepted_by == corg.admin.user_id
    async with tenant_session(corg.tenant_id) as db:
        again = await commercials_service.mark_milestone_billable(corg.ctx(), db, mid, actor=user)
        assert again.status == "accepted" and again.accepted_at == out.accepted_at
    history = await ok(await corg.c.get(f"{API}/contracts/{sow['id']}/history"))
    assert sum("milestone accepted (billable)" in h["body"] for h in history) == 1


async def test_list_milestones_by_project(corg: COrg) -> None:
    from app.models.projects import Project

    sow, m = await _sow_with_milestone(corg)
    pid, other = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(corg.tenant_id) as db:
        db.add(Project(id=pid, tenant_id=corg.tenant_id, human_id="PRJ-T1", name="P", client_id=corg.client_id, contract_id=uuid.UUID(sow["id"])))
        db.add(Project(id=other, tenant_id=corg.tenant_id, human_id="PRJ-T2", name="Q", client_id=corg.client_id))
    async with tenant_session(corg.tenant_id) as db:
        rows = await milestones.list_milestones(corg.ctx(), db, project_id=pid)
        assert [r.id for r in rows] == [uuid.UUID(m["id"])]
        assert await milestones.list_milestones(corg.ctx(), db, project_id=other) == []

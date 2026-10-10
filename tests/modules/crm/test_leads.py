# ruff: noqa: E501
"""Epic 15 story 2.1: settings / codes / sources, leads, duplicate detection, the Lead Status machine and the
first-contact system transition."""

import uuid

import pytest

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.core.permissions import CATALOG, Principal, Scope
from app.schemas.crm import LeadTransitionIn
from app.services.crm import leads as leads_svc
from tests.modules.crm.conftest import API, CrmOrg, JoinAs, create_client, create_lead, err, h, ok, transition_lead

ACTORS = ("user", "system", "ai", "workflow")
STATUSES = ("new", "contacted", "qualified", "converted", "disqualified")
EXPECTED = {
    ("new", "contacted"): {"user", "system"},
    ("new", "qualified"): {"user"},
    ("contacted", "qualified"): {"user"},
    ("qualified", "converted"): {"user"},
    ("new", "disqualified"): {"user"},
    ("contacted", "disqualified"): {"user"},
    ("qualified", "disqualified"): {"user"},
    ("disqualified", "new"): {"user"},
}


@pytest.mark.parametrize("frm", STATUSES)
@pytest.mark.parametrize("to", STATUSES)
@pytest.mark.parametrize("actor", ACTORS)
def test_lead_transition_matrix(frm: str, to: str, actor: str) -> None:
    assert leads_svc.can_transition(frm, to, actor) == (actor in EXPECTED.get((frm, to), set()))


def test_lead_create_actors() -> None:
    assert leads_svc.can_transition(None, "new", "user")
    assert leads_svc.can_transition(None, "new", "system")
    assert not leads_svc.can_transition(None, "new", "ai")
    assert not leads_svc.can_transition(None, "new", "workflow")


async def test_settings_seeded_and_updatable(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    settings = await ok(await c.get(f"{API}/crm/settings"))
    assert settings["stage_probabilities"]["proposal"] == 40
    assert settings["default_currency"] == "INR"
    upd = await ok(await c.patch(f"{API}/crm/settings", json={"stage_probabilities": {"proposal": 35}}, headers=hh))
    assert upd["stage_probabilities"]["proposal"] == 35
    codes = await ok(await c.get(f"{API}/crm/reason-codes", params={"kind": "lead_disqualify"}))
    assert {x["code"] for x in codes} >= {"no_budget", "duplicate", "other"}
    sources = await ok(await c.get(f"{API}/crm/lead-sources"))
    assert "linkedin" in {s["code"] for s in sources}
    new = await ok(await c.post(f"{API}/crm/lead-sources", json={"code": "webinar", "label": "Webinar"}, headers=hh), 201)
    archived = await ok(await c.patch(f"{API}/crm/lead-sources/{new['id']}", json={"archived": True}, headers=hh))
    assert archived["archived_at"] is not None
    rc = await ok(
        await c.post(f"{API}/crm/reason-codes", json={"kind": "opportunity_lost", "code": "budget_cut", "label": "Budget cut"}, headers=hh),
        201,
    )
    assert rc["code"] == "budget_cut"


async def test_duplicates_against_clients_and_leads(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    client = await create_client(corg.admin, "Acme Corp", website="https://acme.com", gstin="27AAPFU0939F1ZV")
    dup = await c.post(f"{API}/leads", json={"company_name": "acme corp"}, headers=hh)
    assert err(dup) == (409, "possible_duplicate")
    matches = dup.json()["error"]["details"]["matches"]
    assert {"kind": "client", "id": client["id"], "name": "Acme Corp", "reason": "name", "human_id": None} in matches
    # acknowledged -> saved, ack audited on the row
    lead = await create_lead(corg.admin, "acme corp", acknowledge_duplicates=True, website="https://www.acme-labs.com/x")
    assert lead["website_domain"] == "acme-labs.com"
    # domain match against a lead
    found = await ok(await c.post(f"{API}/leads/duplicate-check", json={"company_name": "Zeta", "website": "acme-labs.com"}, headers=hh))
    assert [(m["kind"], m["reason"]) for m in found] == [("lead", "domain")]
    # GSTIN match against the client
    found = await ok(await c.post(f"{API}/leads/duplicate-check", json={"company_name": "Other Name", "gstin": "27aapfu0939f1zv"}, headers=hh))
    assert ("client", "gstin") in [(m["kind"], m["reason"]) for m in found]
    # domain against the client website
    found = await ok(await c.post(f"{API}/leads/duplicate-check", json={"company_name": "Nope", "website": "https://www.acme.com/about"}, headers=hh))
    assert ("client", "domain") in [(m["kind"], m["reason"]) for m in found]
    async with tenant_session(corg.tenant_id) as db:
        from sqlalchemy import select

        from app.models.crm import Lead

        row = (await db.execute(select(Lead).where(Lead.id == uuid.UUID(lead["id"])))).scalar_one()
        assert row.duplicate_ack_by == corg.admin.user_id


async def test_lead_lifecycle_and_rules(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    lead = await create_lead(
        corg.admin, "Globex", contact_phone="9812345678", source_code="referral",
        estimated_roles=[{"title": "Java Dev", "positions": 3}], expected_annual_revenue="1200000.50",
    )  # fmt: skip
    assert lead["human_id"].startswith("LEAD-") and len(lead["human_id"]) == 11
    assert lead["contact_phone"] == "+919812345678"
    assert lead["expected_annual_revenue"] == "1200000.50"
    assert lead["status"] == "new"
    # unknown source
    bad = await c.post(f"{API}/leads", json={"company_name": "Bad Source", "source_code": "nope"}, headers=hh)
    assert err(bad) == (422, "invalid_source")
    # disqualify needs a reason; other needs a note
    assert err(await transition_lead(corg.admin, lead["id"], "disqualified")) == (422, "reason_code_required")
    assert err(await transition_lead(corg.admin, lead["id"], "disqualified", reason_code="other")) == (422, "note_required")
    dq = await ok(await transition_lead(corg.admin, lead["id"], "disqualified", reason_code="no_budget"))
    assert dq["status"] == "disqualified" and dq["disqualify_reason_code"] == "no_budget"
    # reopen needs a reopen reason
    assert err(await transition_lead(corg.admin, lead["id"], "new")) == (422, "reason_code_required")
    reopened = await ok(await transition_lead(corg.admin, lead["id"], "new", reason_code="need_revived"))
    assert reopened["status"] == "new" and reopened["disqualify_reason_code"] is None
    # converting directly / via transition is refused
    assert err(await transition_lead(corg.admin, lead["id"], "converted")) == (409, "transition_not_allowed")
    q = await ok(await transition_lead(corg.admin, lead["id"], "qualified"))
    assert q["status"] == "qualified"
    hist = await ok(await c.get(f"{API}/leads/{lead['id']}/history"))
    assert [x["to_value"] for x in hist] == ["new", "disqualified", "new", "qualified"]
    # list + filters
    page = await ok(await c.get(f"{API}/leads", params={"status": "qualified", "q": "glob"}))
    assert [x["id"] for x in page["items"]] == [lead["id"]]
    page = await ok(await c.get(f"{API}/leads", params={"source": "website"}))
    assert page["total"] == 0
    upd = await ok(await c.patch(f"{API}/leads/{lead['id']}", json={"industry": "Retail"}, headers=hh))
    assert upd["industry"] == "Retail"
    archived = await ok(await c.post(f"{API}/leads/{lead['id']}/archive", headers=hh))
    assert archived["archived_at"] is not None
    assert (await ok(await c.get(f"{API}/leads")))["total"] == 0


async def test_ai_and_workflow_actors_refused(corg: CrmOrg) -> None:
    lead = await create_lead(corg.admin, "Initrode")
    principal = Principal(type="user", id=corg.admin.user_id, tenant_id=corg.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT))
    for actor in ("ai", "workflow"):
        ctx = RequestContext(tenant_id=corg.tenant_id, principal=principal, actor_kind=actor)
        async with tenant_session(corg.tenant_id) as db:
            with pytest.raises(ConflictError) as exc:
                await leads_svc.transition_lead(ctx, db, uuid.UUID(lead["id"]), LeadTransitionIn(to="contacted"), actor_kind=actor)
            assert exc.value.code == "transition_not_allowed"


async def test_first_logged_call_moves_new_to_contacted(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    lead = await create_lead(corg.admin, "Hooli")
    note = await c.post(f"{API}/leads/{lead['id']}/activities", json={"kind": "note", "body": "Researching"}, headers=hh)
    assert note.status_code == 201, note.text
    assert (await ok(await c.get(f"{API}/leads/{lead['id']}")))["status"] == "new"  # a note is not a contact
    call = await c.post(f"{API}/leads/{lead['id']}/activities", json={"kind": "call", "body": "Intro call"}, headers=hh)
    assert call.status_code == 201, call.text
    got = await ok(await c.get(f"{API}/leads/{lead['id']}"))
    assert got["status"] == "contacted"
    hist = await ok(await c.get(f"{API}/leads/{lead['id']}/history"))
    assert (hist[-1]["to_value"], hist[-1]["actor_kind"]) == ("contacted", "system")
    # second call: idempotent
    await c.post(f"{API}/leads/{lead['id']}/activities", json={"kind": "call", "body": "Follow-up"}, headers=hh)
    assert len(await ok(await c.get(f"{API}/leads/{lead['id']}/history"))) == 2
    # tasks attach to leads
    task = await c.post(
        f"{API}/tasks", json={"title": "Send deck", "entity_type": "lead", "entity_id": lead["id"]}, headers=hh
    )
    assert task.status_code == 201, task.text


async def test_scope_own_vs_tenant(corg: CrmOrg, join_as: JoinAs) -> None:
    rep = await join_as("sales", name="Rita Rep")
    mine = await create_lead(rep, "Rep Lead")
    admins = await create_lead(corg.admin, "Admin Lead")
    page = await ok(await rep.client.get(f"{API}/leads"))
    assert [x["id"] for x in page["items"]] == [mine["id"]]
    assert (await rep.client.get(f"{API}/leads/{admins['id']}")).status_code == 404
    assert (await ok(await corg.admin.client.get(f"{API}/leads")))["total"] == 2
    recruiter = await join_as("recruiter", name="Ron Recruiter")
    assert (await recruiter.client.get(f"{API}/leads")).status_code == 403

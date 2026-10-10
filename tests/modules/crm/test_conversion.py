# ruff: noqa: E501
"""Epic 15 story 2.2 (A4): lead conversion -- create vs link Client / Contact, ``client_exists``, the converted
Lead is read-only, and the Lead's activities show on the Opportunity's and Client's timelines."""

from typing import Any

from tests.modules.crm.conftest import API, CrmOrg, create_client, create_lead, err, h, ok, transition_lead


async def _qualified(org: CrmOrg, company: str, **extra: Any) -> dict[str, Any]:
    lead = await create_lead(org.admin, company, acknowledge_duplicates=True, **extra)
    return await ok(await transition_lead(org.admin, lead["id"], "qualified"))


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client": {"mode": "create", "name": "Acme Robotics", "website": "acme-robotics.com"},
        "contact": {"mode": "create", "first_name": "Asha", "last_name": "Rao", "email": "asha@acme-robotics.com", "phone": "9812345678", "title": "HR Head"},
        "opportunity": {"name": "Acme staffing 2026", "service": "contract_staffing", "estimated_revenue": "500000.00",
                        "lines": [{"title": "Java Developer", "positions": 2, "expected_bill_rate": "1500.0000", "rate_unit": "hour"}]},
    }  # fmt: skip
    body.update(over)
    return body


async def test_convert_creating_client_and_contact(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    lead = await create_lead(corg.admin, "Acme Robotics Pvt")
    # a New lead cannot be converted
    assert err(await c.post(f"{API}/leads/{lead['id']}/convert", json=_body(), headers=hh)) == (409, "transition_not_allowed")
    call = await c.post(f"{API}/leads/{lead['id']}/activities", json={"kind": "call", "body": "Discovery call"}, headers=hh)
    assert call.status_code == 201
    assert (await ok(await c.get(f"{API}/leads/{lead['id']}")))["status"] == "contacted"
    await ok(await transition_lead(corg.admin, lead["id"], "qualified"))
    out = await ok(await c.post(f"{API}/leads/{lead['id']}/convert", json=_body(), headers=hh), 201)
    assert out["lead"]["status"] == "converted"
    assert out["lead"]["converted_opportunity_id"] == out["opportunity"]["id"]
    assert out["lead"]["converted_client_id"] == out["client_id"]
    assert out["lead"]["converted_contact_id"] == out["contact_id"]
    opp = out["opportunity"]
    assert opp["lead_id"] == lead["id"] and opp["stage"] == "qualification" and opp["probability_pct"] == "10.00"
    assert opp["primary_contact_id"] == out["contact_id"]
    assert [ln["title"] for ln in opp["lines"]] == ["Java Developer"]
    client = await ok(await c.get(f"{API}/clients/{out['client_id']}"))
    assert (client["name"], client["status"]) == ("Acme Robotics", "prospect")
    # read-only lead
    assert err(await c.patch(f"{API}/leads/{lead['id']}", json={"industry": "X"}, headers=hh)) == (409, "lead_converted")
    assert err(await transition_lead(corg.admin, lead["id"], "disqualified", reason_code="no_need")) == (409, "lead_converted")
    # notes still allowed on the lead
    note = await c.post(f"{API}/leads/{lead['id']}/activities", json={"kind": "note", "body": "Post-conversion"}, headers=hh)
    assert note.status_code == 201
    # the lead's call shows on the opportunity and client timelines
    for path in (f"opportunities/{opp['id']}", f"clients/{out['client_id']}"):
        tl = await ok(await c.get(f"{API}/{path}/timeline", params={"limit": 50}))
        bodies = [i["body"] for i in tl["items"]]
        assert "Discovery call" in bodies, (path, bodies)


async def test_convert_client_exists_then_link(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    existing = await create_client(corg.admin, "Acme Robotics")
    contact = await ok(await c.post(f"{API}/clients/{existing['id']}/contacts", json={"name": "Ken Contact"}, headers=hh), 201)
    lead = await _qualified(corg, "Acme R")
    resp = await c.post(f"{API}/leads/{lead['id']}/convert", json=_body(), headers=hh)
    assert err(resp) == (409, "client_exists")
    assert resp.json()["error"]["details"]["client_id"] == existing["id"]
    # nothing was created by the failed attempt
    assert (await ok(await c.get(f"{API}/leads/{lead['id']}")))["status"] == "qualified"
    body = _body(client={"mode": "link", "client_id": existing["id"]}, contact={"mode": "link", "contact_id": contact["id"]})
    out = await ok(await c.post(f"{API}/leads/{lead['id']}/convert", json=body, headers=hh), 201)
    assert out["client_id"] == existing["id"] and out["contact_id"] == contact["id"]
    # a contact of another client is refused
    other_client = await create_client(corg.admin, "Other Co")
    lead2 = await _qualified(corg, "Lead Two")
    bad = _body(client={"mode": "link", "client_id": other_client["id"]}, contact={"mode": "link", "contact_id": contact["id"]})
    assert err(await c.post(f"{API}/leads/{lead2['id']}/convert", json=bad, headers=hh)) == (422, "invalid_reference")


async def test_convert_without_contact_and_inactive_client(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    inactive = await create_client(corg.admin, "Sleepy Co", status="inactive")
    lead = await _qualified(corg, "Sleepy")
    body = _body(client={"mode": "link", "client_id": inactive["id"]}, contact={"mode": "none"})
    assert err(await c.post(f"{API}/leads/{lead['id']}/convert", json=body, headers=hh)) == (409, "client_not_open")
    active = await create_client(corg.admin, "Awake Co", status="active")
    body = _body(client={"mode": "link", "client_id": active["id"]}, contact={"mode": "none"})
    out = await ok(await c.post(f"{API}/leads/{lead['id']}/convert", json=body, headers=hh), 201)
    assert out["contact_id"] is None and out["opportunity"]["primary_contact_id"] is None

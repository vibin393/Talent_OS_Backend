"""Epic 2 (stories 2.1 / 2.2): clients and contacts -- CRUD, duplicates, GSTIN, archive, scope, 360,
cross-tenant 404s and audit."""

import uuid
from typing import Any

import httpx
import pytest

from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound
from app.services import clients_service
from tests.epic2 import JoinAs
from tests.modules.identity.conftest import Org

CLIENTS = "/api/v1/clients"
CONTACTS = "/api/v1/contacts"
ROLES = "/api/v1/admin/roles"


async def _client(
    member_client: httpx.AsyncClient, h: dict[str, str], name: str, **extra: Any
) -> dict[str, Any]:
    resp = await member_client.post(CLIENTS, json={"name": name, **extra}, headers=h)
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


# ---------------------------------------------------------------- create / duplicates / validation
async def test_create_client_with_similar_name_warning_and_duplicate(org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    first = await _client(
        c,
        h,
        "Globex Corporation",
        industry="Manufacturing",
        website="globex.example.com",
        locations=[{"city": "Pune", "state": "MH", "country": "India"}],
        gstin="27aapfu0939f1zv",
        status="active",
    )
    assert first["warnings"] == []
    assert first["owner_id"] == str(org.admin.user_id)
    assert first["owner"] == {"id": str(org.admin.user_id), "name": "Ada Admin"}
    assert first["website"] == "https://globex.example.com"
    assert first["gstin"] == "27AAPFU0939F1ZV"
    assert first["locations"] == [{"city": "Pune", "state": "MH", "country": "India"}]

    similar = await _client(c, h, "Globex Corporation Ltd")
    assert [w["name"] for w in similar["warnings"]] == ["Globex Corporation"]
    assert similar["warnings"][0]["similarity"] >= 0.6

    dup = await c.post(CLIENTS, json={"name": "GLOBEX corporation"}, headers=h)
    assert (dup.status_code, dup.json()["error"]["code"]) == (409, "client_exists")


@pytest.mark.parametrize("gstin", ["12ABC", "27AAPFU0939F1ZX1", "2AAPFU0939F1ZV0"])
async def test_bad_gstin_is_a_field_error(org: Org, gstin: str) -> None:
    resp = await org.admin.client.post(
        CLIENTS, json={"name": "Initrode", "gstin": gstin}, headers=org.admin.h()
    )
    assert resp.status_code == 422
    fields = [f["field"] for f in resp.json()["error"]["details"]["fields"]]
    assert fields == ["body.gstin"]


async def test_unknown_or_foreign_owner_is_invalid_reference(org: Org, other: Org) -> None:
    for owner in (uuid.uuid4(), other.admin.user_id):
        resp = await org.admin.client.post(
            CLIENTS, json={"name": f"Owned {owner}", "owner_id": str(owner)}, headers=org.admin.h()
        )
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_reference")


async def test_update_client_and_rename_conflict(org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    a = await _client(c, h, "Alpha Works")
    await _client(c, h, "Beta Works")
    ok = await c.patch(f"{CLIENTS}/{a['id']}", json={"industry": "IT", "status": "inactive"}, headers=h)
    assert ok.status_code == 200, ok.text
    assert (ok.json()["industry"], ok.json()["status"]) == ("IT", "inactive")
    clash = await c.patch(f"{CLIENTS}/{a['id']}", json={"name": "beta works"}, headers=h)
    assert (clash.status_code, clash.json()["error"]["code"]) == (409, "client_exists")
    case_only = await c.patch(f"{CLIENTS}/{a['id']}", json={"name": "ALPHA Works"}, headers=h)
    assert case_only.json()["name"] == "ALPHA Works"


# ---------------------------------------------------------------- archive
async def test_archive_hides_client_and_blocks_demands(org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    client = await _client(c, h, "Soylent Archive Co")
    archived = await c.post(f"{CLIENTS}/{client['id']}/archive", headers=h)
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    listed = (await c.get(CLIENTS)).json()
    assert client["id"] not in [i["id"] for i in listed["items"]]
    with_archived = (await c.get(CLIENTS, params={"include_archived": "true"})).json()
    assert client["id"] in [i["id"] for i in with_archived["items"]]

    async with tenant_session(org.tenant.id) as db:
        with pytest.raises(ConflictError) as exc:
            await clients_service.assert_client_accepts_demands(db, uuid.UUID(client["id"]))
        assert exc.value.code == "client_archived"
        with pytest.raises(NotFound):
            await clients_service.assert_client_accepts_demands(db, uuid.uuid4())

    restored = await c.post(f"{CLIENTS}/{client['id']}/unarchive", headers=h)
    assert restored.json()["archived_at"] is None
    async with tenant_session(org.tenant.id) as db:
        await clients_service.assert_client_accepts_demands(db, uuid.UUID(client["id"]))


async def test_list_filters_and_sorting(org: Org, join_as: JoinAs) -> None:
    c, h = org.admin.client, org.admin.h()
    am = await join_as("account_manager", name="Amy Account")
    await _client(c, h, "Zeta Foods", status="active")
    await _client(c, h, "Eta Foods", status="prospect", owner_id=str(am.user_id))
    names = [i["name"] for i in (await c.get(CLIENTS, params={"q": "foods"})).json()["items"]]
    assert names == ["Eta Foods", "Zeta Foods"]
    desc = [i["name"] for i in (await c.get(CLIENTS, params={"q": "foods", "sort": "-name"})).json()["items"]]
    assert desc == ["Zeta Foods", "Eta Foods"]
    active = (await c.get(CLIENTS, params={"status": "active"})).json()["items"]
    assert [i["name"] for i in active] == ["Zeta Foods"]
    owned = (await c.get(CLIENTS, params={"owner_id": str(am.user_id)})).json()["items"]
    assert [i["name"] for i in owned] == ["Eta Foods"]
    assert (await c.get(CLIENTS, params={"sort": "bogus"})).status_code == 422
    hits = (await c.get(f"{CLIENTS}/quick-search", params={"q": "zeta"})).json()
    assert [(x["kind"], x["title"]) for x in hits] == [("client", "Zeta Foods")]


# ---------------------------------------------------------------- scope
async def test_own_scope_lists_only_own_clients(org: Org, join_as: JoinAs) -> None:
    role = await org.admin.client.post(
        ROLES,
        json={
            "key": "own_clients",
            "name": "Own clients",
            "permissions": {"client:read": "own", "client:create": "own", "client:update": "own"},
        },
        headers=org.admin.h(),
    )
    assert role.status_code == 201, role.text
    me = await join_as("own_clients", name="Olive Own")
    mine = await _client(me.client, me.h(), "Mine Industries")
    theirs = await _client(org.admin.client, org.admin.h(), "Theirs Industries")

    listed = (await me.client.get(CLIENTS)).json()
    assert [i["id"] for i in listed["items"]] == [mine["id"]]
    assert (await me.client.get(f"{CLIENTS}/{theirs['id']}")).status_code == 404
    upd = await me.client.patch(f"{CLIENTS}/{theirs['id']}", json={"industry": "x"}, headers=me.h())
    assert upd.status_code == 404
    assert (
        await me.client.patch(f"{CLIENTS}/{mine['id']}", json={"industry": "x"}, headers=me.h())
    ).status_code == 200


async def test_account_manager_updates_only_own_clients(org: Org, join_as: JoinAs) -> None:
    am = await join_as("account_manager", name="Amy Account")
    mine = await _client(am.client, am.h(), "Amy's Client")
    theirs = await _client(org.admin.client, org.admin.h(), "Admin's Client")
    # Visible (client:read tenant) but not editable (client:update own) -> 403.
    assert (await am.client.get(f"{CLIENTS}/{theirs['id']}")).status_code == 200
    denied = await am.client.patch(f"{CLIENTS}/{theirs['id']}", json={"industry": "x"}, headers=am.h())
    assert (denied.status_code, denied.json()["error"]["details"]["permission"]) == (403, "client:update")
    assert (await am.client.post(f"{CLIENTS}/{theirs['id']}/archive", headers=am.h())).status_code == 403
    assert (await am.client.post(f"{CLIENTS}/{mine['id']}/archive", headers=am.h())).status_code == 200


async def test_recruiter_cannot_create_clients(join_as: JoinAs) -> None:
    rec = await join_as("recruiter", name="Rick Recruiter")
    resp = await rec.client.post(CLIENTS, json={"name": "Nope"}, headers=rec.h())
    assert (resp.status_code, resp.json()["error"]["details"]["permission"]) == (403, "client:create")


# ---------------------------------------------------------------- contacts
async def test_contacts_crud_duplicates_and_phone(org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    acme = await _client(c, h, "Acme Contacts Inc")
    other_client = await _client(c, h, "Other Contacts Inc")
    created = await c.post(
        f"{CLIENTS}/{acme['id']}/contacts",
        json={
            "name": "Hana Hiring",
            "title": "Engineering Manager",
            "email": "Hana@Acme.Example.com",
            "phone": "098765 43210",
            "type": "hiring_manager",
        },
        headers=h,
    )
    assert created.status_code == 201, created.text
    contact = created.json()
    assert (contact["email"], contact["phone"], contact["type"]) == (
        "hana@acme.example.com",
        "+919876543210",
        "hiring_manager",
    )
    dup = await c.post(
        f"{CLIENTS}/{acme['id']}/contacts", json={"name": "Dup", "email": "HANA@acme.example.com"}, headers=h
    )
    assert (dup.status_code, dup.json()["error"]["code"]) == (409, "contact_exists")
    elsewhere = await c.post(
        f"{CLIENTS}/{other_client['id']}/contacts",
        json={"name": "Same Email", "email": "hana@acme.example.com"},
        headers=h,
    )
    assert elsewhere.status_code == 201
    bad_phone = await c.post(
        f"{CLIENTS}/{acme['id']}/contacts", json={"name": "Bad", "phone": "12"}, headers=h
    )
    assert bad_phone.status_code == 422

    second = await c.post(
        f"{CLIENTS}/{acme['id']}/contacts", json={"name": "Finn Finance", "type": "finance"}, headers=h
    )
    assert second.status_code == 201
    listed = (await c.get(f"{CLIENTS}/{acme['id']}/contacts")).json()
    assert [i["name"] for i in listed["items"]] == ["Finn Finance", "Hana Hiring"]

    patched = await c.patch(f"{CONTACTS}/{contact['id']}", json={"title": "VP Engineering"}, headers=h)
    assert patched.json()["title"] == "VP Engineering"
    clash = await c.patch(
        f"{CONTACTS}/{second.json()['id']}", json={"email": "hana@acme.example.com"}, headers=h
    )
    assert clash.json()["error"]["code"] == "contact_exists"

    assert (await c.post(f"{CONTACTS}/{contact['id']}/archive", headers=h)).status_code == 200
    listed = (await c.get(f"{CLIENTS}/{acme['id']}/contacts")).json()
    assert [i["name"] for i in listed["items"]] == ["Finn Finance"]
    all_items = (await c.get(f"{CLIENTS}/{acme['id']}/contacts", params={"include_archived": "true"})).json()
    assert all_items["total"] == 2
    assert (await c.post(f"{CONTACTS}/{contact['id']}/unarchive", headers=h)).json()["archived_at"] is None
    async with tenant_session(org.tenant.id) as db:
        assert await clients_service.contact_belongs_to_client(
            db, uuid.UUID(contact["id"]), uuid.UUID(acme["id"])
        )
        assert not await clients_service.contact_belongs_to_client(
            db, uuid.UUID(contact["id"]), uuid.UUID(other_client["id"])
        )


async def test_client_360(org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    acme = await _client(c, h, "Acme 360")
    for name in ("Carla Contact", "Dan Contact"):
        await c.post(f"{CLIENTS}/{acme['id']}/contacts", json={"name": name}, headers=h)
    await c.post(f"{CLIENTS}/{acme['id']}/activities", json={"body": "Kickoff call went well"}, headers=h)
    resp = await c.get(f"{CLIENTS}/{acme['id']}/360")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client"]["name"] == "Acme 360"
    assert [x["name"] for x in body["contacts"]] == ["Carla Contact", "Dan Contact"]
    assert body["counts"]["contacts"] == 2
    assert body["counts"]["activities"] == 4  # created + 2 contacts added + 1 note
    assert body["demands"] == [] and body["submissions"] == []


# ---------------------------------------------------------------- isolation
async def test_every_endpoint_is_404_across_tenants(org: Org, other: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    client = await _client(c, h, "Private Tenant Client")
    contact = (
        await c.post(f"{CLIENTS}/{client['id']}/contacts", json={"name": "Secret Person"}, headers=h)
    ).json()
    note = (await c.post(f"{CLIENTS}/{client['id']}/activities", json={"body": "hi"}, headers=h)).json()
    o, oh = other.admin.client, other.admin.h()
    cid, kid = client["id"], contact["id"]
    requests = [
        o.get(f"{CLIENTS}/{cid}"),
        o.get(f"{CLIENTS}/{cid}/360"),
        o.patch(f"{CLIENTS}/{cid}", json={"industry": "x"}, headers=oh),
        o.post(f"{CLIENTS}/{cid}/archive", headers=oh),
        o.post(f"{CLIENTS}/{cid}/unarchive", headers=oh),
        o.get(f"{CLIENTS}/{cid}/contacts"),
        o.post(f"{CLIENTS}/{cid}/contacts", json={"name": "x"}, headers=oh),
        o.get(f"{CONTACTS}/{kid}"),
        o.patch(f"{CONTACTS}/{kid}", json={"title": "x"}, headers=oh),
        o.post(f"{CONTACTS}/{kid}/archive", headers=oh),
        o.post(f"{CONTACTS}/{kid}/unarchive", headers=oh),
        o.get(f"{CLIENTS}/{cid}/timeline"),
        o.post(f"{CLIENTS}/{cid}/activities", json={"body": "x"}, headers=oh),
        o.get(f"{CONTACTS}/{kid}/timeline"),
        o.patch(f"/api/v1/activities/{note['id']}", json={"body": "x"}, headers=oh),
    ]
    for pending in requests:
        resp = await pending
        assert resp.status_code == 404, (resp.request.method, resp.request.url, resp.text)
    assert (await o.get(CLIENTS)).json()["total"] == 0
    assert (await o.get(f"{CLIENTS}/quick-search", params={"q": "private"})).json() == []


async def test_changes_are_in_the_audit_log(org: Org) -> None:
    c, h = org.admin.client, org.admin.h()
    client = await _client(c, h, "Audited Client")
    contact = (
        await c.post(
            f"{CLIENTS}/{client['id']}/contacts",
            json={"name": "Audrey Audit", "email": "a@x.example"},
            headers=h,
        )
    ).json()
    await c.patch(f"{CLIENTS}/{client['id']}", json={"industry": "Retail"}, headers=h)
    logs = (await c.get("/api/v1/admin/audit-logs", params={"entity_id": client["id"]})).json()["items"]
    actions = {i["action"] for i in logs}
    assert {"client.created", "client.updated"} <= actions
    updated = next(i for i in logs if i["action"] == "client.updated")
    assert updated["changes"]["industry"] == {"old": None, "new": "Retail"}
    contact_logs = (await c.get("/api/v1/admin/audit-logs", params={"entity_id": contact["id"]})).json()[
        "items"
    ]
    created = next(i for i in contact_logs if i["action"] == "contact.created")
    assert created["changes"]["name"]["new"] == "Audrey Audit"  # PII decrypted for the admin

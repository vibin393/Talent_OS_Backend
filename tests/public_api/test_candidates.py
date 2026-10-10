# ruff: noqa: E501
"""Epic 67 (story 67.5): ``/public/v1/candidates`` -- list / keyset paging, get, create (consent, Idempotency-Key),
PATCH (If-Match 428 / 412), masking without ``pii.*``, audit actor ``app`` with ``on_behalf_of`` and exposures."""

from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.services import exposure_service
from tests.epic3 import Org3, create_candidate
from tests.public_api.e67 import CONSENT, Api, app_token, code, installed, user_token


async def test_create_get_patch_and_masking(app: FastAPI, org3: Org3) -> None:
    inst = await installed(org3)
    api = Api(app, await user_token(inst, org3.admin.user_id))
    body = {"full_name": "Asha Rao", "email": "asha@example.com", "phone": "+919812345678", "consent": CONSENT}
    missing = await api.post("/candidates", {k: v for k, v in body.items() if k != "consent"})
    assert (missing.status_code, code(missing)) == (422, "consent_basis_required")
    key = "cand-1"
    resp = await api.post("/candidates", body, key=key)
    assert resp.status_code == 201, resp.text
    cand = resp.json()
    assert cand["object"] == "candidate" and cand["email"] == "asha@example.com" and cand["source"] == "public_api"
    assert cand["owner_id"] == str(org3.admin.user_id)
    # replay with the same key returns the stored response; a different body -> 409
    again = await api.post("/candidates", body, key=key)
    assert again.status_code == 201 and again.json()["id"] == cand["id"]
    clash = await api.post("/candidates", {**body, "full_name": "Other"}, key=key)
    assert (clash.status_code, code(clash)) == (409, "idempotency_key_reused")
    # PATCH needs If-Match
    path = f"/candidates/{cand['id']}"
    assert code(await api.patch(path, {"current_title": "Lead"})) == "precondition_required"
    assert (await api.patch(path, {"current_title": "Lead"}, if_match='W/"1"')).status_code == 412
    ok = await api.patch(path, {"current_title": "Lead"}, if_match=f'W/"{cand["version"]}"')
    assert ok.status_code == 200, ok.text
    assert ok.json()["current_title"] == "Lead"
    # app-only token: contact masked and listed
    plain = Api(app, await app_token(inst))
    got = (await plain.get(path)).json()
    assert got["email"] is None and got["phone"] is None
    assert {"email", "phone"} <= set(got["masked_fields"])
    # audit: actor app on behalf of the user
    async with tenant_session(org3.tenant_id) as db:
        row = (await db.execute(text("SELECT actor_type, on_behalf_of_user_id FROM audit_logs WHERE action = 'public_api.candidate_consent'"))).first()
    assert row is not None and row[0] == "app" and row[1] == org3.admin.user_id
    await exposure_service.flush()
    async with tenant_session(org3.tenant_id) as db:
        n = (await db.execute(text("SELECT count(*) FROM subject_exposures WHERE installation_id = :i"), {"i": inst.id})).scalar_one()
    assert n >= 1


async def test_list_keyset_paging_stable_under_inserts(app: FastAPI, org3: Org3) -> None:
    inst = await installed(org3)
    for n in range(5):
        await create_candidate(org3.admin, full_name=f"Cand {n}")
    api = Api(app, await app_token(inst))
    first = (await api.get("/candidates", limit=2)).json()
    assert len(first["data"]) == 2 and first["has_more"] and first["next_cursor"]
    await create_candidate(org3.admin, full_name="Late arrival")  # sorts after everything seen so far
    seen = [c["id"] for c in first["data"]]
    cursor = first["next_cursor"]
    while cursor:
        page = (await api.get("/candidates", limit=2, cursor=cursor)).json()
        seen += [c["id"] for c in page["data"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 6
    bad = await api.get("/candidates", cursor="tampered.cursor")
    assert (bad.status_code, code(bad)) == (400, "invalid_cursor")
    desc = (await api.get("/candidates", order="desc", limit=1)).json()
    assert desc["data"][0]["full_name"] == "Late arrival"


async def test_cross_tenant_and_out_of_scope_404(app: FastAPI, org3: Org3, other3: Org3) -> None:
    from tests.epic3 import member

    cand = await create_candidate(org3.admin, full_name="Hidden")
    other_inst = await installed(other3)
    other = Api(app, await app_token(other_inst))
    assert (await other.get(f"/candidates/{cand['id']}")).status_code == 404
    # a recruiter with own-scope candidate:read sees only her own candidates
    rita = await member(app, org3, "recruiter")
    inst = await installed(org3)
    api = Api(app, await user_token(inst, rita.user_id, scopes=("candidates:read",)))
    resp = await api.get(f"/candidates/{cand['id']}")
    from app.services import identity_bridge

    async with tenant_session(org3.tenant_id) as db:
        scope = (await identity_bridge.user_permissions(db, rita.user_id)).get("candidate:read")
    if scope in ("own", "team"):
        assert resp.status_code == 404
    _ = select

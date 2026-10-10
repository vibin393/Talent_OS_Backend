# ruff: noqa: E501
"""Epic 67 (NFR-60, spec A8): for every public read operation -- cross-tenant id 404, masked fields without ``pii.*``,
deactivated user 401, revoked authorisation 401, suspended Installation 401, kill switch 403, missing scope 403."""

import pytest
from fastapi import FastAPI
from sqlalchemy import update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.models.identity import User
from app.models.oauth import AppInstallation, AppUserAuthorization
from app.services import ecosystem_service, oauth_service
from tests.epic3 import Org3, create_candidate
from tests.public_api.e67 import Api, app_token, code, installed, user_token
from tests.public_api.test_resources import READS

PATHS = [p for p, _ in READS]


def _expire() -> None:
    oauth_service.clear_cache()
    ecosystem_service.bust_cache()


async def _all(api: Api) -> dict[str, tuple[int, str | None]]:
    out = {}
    for path in PATHS:
        resp = await api.get(path)
        out[path] = (resp.status_code, code(resp))
    return out


async def test_cross_tenant_and_masking(app: FastAPI, org3: Org3, other3: Org3) -> None:
    cand = await create_candidate(org3.admin, full_name="Iso One", emails=[{"email": "iso@example.com", "is_primary": True}])
    mine = Api(app, await app_token(await installed(org3)))
    theirs = Api(app, await app_token(await installed(other3)))
    assert (await theirs.get(f"/candidates/{cand['id']}")).status_code == 404
    listed = (await theirs.get("/candidates")).json()["data"]
    assert all(c["id"] != cand["id"] for c in listed)
    got = (await mine.get(f"/candidates/{cand['id']}")).json()
    assert got["email"] is None and "email" in got["masked_fields"]


@pytest.mark.parametrize("failure", ["deactivated", "revoked_authorization", "suspended_installation", "kill_switch"])
async def test_failures_apply_to_every_operation(app: FastAPI, org3: Org3, failure: str) -> None:
    from tests.epic3 import member

    rita = await member(app, org3, "recruiter")
    inst = await installed(org3)
    api = Api(app, await user_token(inst, rita.user_id))
    expected: tuple[int, str | None]
    async with tenant_session(org3.tenant_id) as db:
        if failure == "deactivated":
            await db.execute(update(User).where(User.id == rita.user_id).values(is_active=False))
            expected = (401, "invalid_token")
        elif failure == "revoked_authorization":
            await db.execute(update(AppUserAuthorization).where(AppUserAuthorization.user_id == rita.user_id).values(revoked_at=AppUserAuthorization.granted_at))
            expected = (401, "invalid_token")
        elif failure == "suspended_installation":
            await db.execute(update(AppInstallation).where(AppInstallation.id == inst.id).values(status="suspended"))
            expected = (401, "invalid_token")
        else:
            human = RequestContext(tenant_id=org3.tenant_id, principal=Principal(type="user", id=org3.admin.user_id, tenant_id=org3.tenant_id))
            await ecosystem_service.set_kill_switch(human, db, on=True, reason="incident")
            expected = (403, "ecosystem_suspended")
    _expire()
    results = await _all(api)
    assert all(v == expected for v in results.values()), results


async def test_missing_scope_on_every_operation(app: FastAPI, org3: Org3) -> None:
    inst = await installed(org3, scopes=("skills:read",))
    results = await _all(Api(app, await app_token(inst, scopes=("skills:read",))))
    assert all(v == (403, "insufficient_scope") for v in results.values()), results


async def test_delegated_user_lost_permission(app: FastAPI, org3: Org3, monkeypatch: pytest.MonkeyPatch) -> None:
    """The token lists candidates:read but the user no longer holds candidate:read -> 403 forbidden."""
    import dataclasses

    from app.services import entity_refs
    from tests.epic3 import member

    viewer = await member(app, org3, "recruiter")
    inst = await installed(org3)
    token = await user_token(inst, viewer.user_id, scopes=("candidates:read",))
    real = entity_refs.principal_for_user

    async def stripped(db, tenant_id, user_id):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        p = await real(db, tenant_id, user_id)
        return None if p is None else dataclasses.replace(p, permissions={k: v for k, v in p.permissions.items() if k != "candidate:read"})

    monkeypatch.setattr(entity_refs, "principal_for_user", stripped)
    _expire()
    resp = await Api(app, token).get("/candidates")
    assert (resp.status_code, code(resp)) == (403, "forbidden")

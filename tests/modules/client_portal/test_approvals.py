"""Epic 18 FR-102: approvals hub, generic decide, delegation and Secure Link approvals."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from freezegun import freeze_time
from sqlalchemy import select

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.models.approvals import ApprovalDecision, ApprovalRequest
from app.services import approvals
from app.services.approvals import FinalDecision, StepSpec
from app.services.client_portal import links
from tests.modules.client_portal.helpers import CP, client_user, idem
from tests.modules.pipeline.helpers import POrg

SUBJECT = "cp_test_subject"


class Calls:
    def __init__(self) -> None:
        self.finals: list[FinalDecision] = []
        self.fail = False


@pytest.fixture
async def subject() -> AsyncIterator[Calls]:
    c = Calls()

    async def on_final(ctx: RequestContext, db: Any, d: FinalDecision) -> None:
        if c.fail:
            raise RuntimeError("callback failed")
        c.finals.append(d)

    approvals.register_approval_subject(SUBJECT, on_final_decided=on_final)
    yield c
    approvals.unregister_approval_subject(SUBJECT)


def _ctx(org: POrg) -> RequestContext:
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(
            type="user",
            id=org.admin.user_id,
            tenant_id=org.tenant_id,
            permissions=dict.fromkeys(CATALOG, Scope.TENANT),
        ),
    )


async def _start(org: POrg, steps: list[StepSpec], subject_type: str = SUBJECT) -> uuid.UUID:
    async with tenant_session(org.tenant_id) as db:
        result = await approvals.start(
            _ctx(org), db, subject_type, uuid.uuid4(), {}, steps=steps, initiated_by=ActorRef("system", None)
        )
    assert result.request_id is not None
    return result.request_id


def _to(cu: uuid.UUID) -> list[StepSpec]:
    return [StepSpec(name="Client", approver_kind="client_user", approver_ref={"client_user_id": str(cu)})]


async def _status(org: POrg, rid: uuid.UUID) -> str:
    async with tenant_session(org.tenant_id) as db:
        return str(
            (await db.execute(select(ApprovalRequest.status).where(ApprovalRequest.id == rid))).scalar_one()
        )


async def test_hub_detail_and_generic_decide(app: FastAPI, porg: POrg, subject: Calls) -> None:
    dev = await client_user(app, porg.tenant_id, porg.client_id)
    other = await client_user(app, porg.tenant_id, porg.client_id)
    rid = await _start(porg, _to(dev.id))

    hub = (await dev.client.get(f"{CP}/approvals")).json()
    assert hub["total"] == 1 and hub["kinds"][0]["items"][0]["request_id"] == str(rid)
    assert (await other.client.get(f"{CP}/approvals")).json()["total"] == 0
    assert (await other.client.get(f"{CP}/approvals/{rid}")).status_code == 404
    assert (await dev.client.get(f"{CP}/approvals/{rid}")).status_code == 200

    no_key = await dev.client.post(
        f"{CP}/approvals/{rid}/decide", json={"decision": "approve"}, headers=dev.h()
    )
    assert no_key.status_code == 422
    rej = await dev.client.post(
        f"{CP}/approvals/{rid}/decide", json={"decision": "reject"}, headers={**dev.h(), **idem()}
    )
    assert rej.status_code == 422 and rej.json()["error"]["code"] == "comment_required"

    # forced callback failure rolls back the decision too (AD-24)
    subject.fail = True
    try:
        failed = await dev.client.post(
            f"{CP}/approvals/{rid}/decide", json={"decision": "approve"}, headers={**dev.h(), **idem()}
        )
        assert failed.status_code == 500
    except RuntimeError:
        pass
    assert await _status(porg, rid) == "pending"
    subject.fail = False

    ok = await dev.client.post(
        f"{CP}/approvals/{rid}/decide", json={"decision": "approve"}, headers={**dev.h(), **idem()}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"
    assert subject.finals[-1].actor is not None and subject.finals[-1].actor.kind == "client_user"


async def test_timesheets_use_their_own_endpoint(app: FastAPI, porg: POrg) -> None:
    dev = await client_user(app, porg.tenant_id, porg.client_id)
    rid = await _start(porg, _to(dev.id), subject_type="timesheet")
    hub = (await dev.client.get(f"{CP}/approvals")).json()
    item = hub["kinds"][0]["items"][0]
    assert item["kind"] == "timesheet" and item["decide_via"] == "subject_endpoint"
    assert item["link"].startswith("/client/timesheets/")
    resp = await dev.client.post(
        f"{CP}/approvals/{rid}/decide", json={"decision": "approve"}, headers={**dev.h(), **idem()}
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "use_subject_endpoint"


async def test_delegation_rules_and_on_behalf_of(app: FastAPI, porg: POrg, subject: Calls) -> None:
    dev = await client_user(app, porg.tenant_id, porg.client_id)
    deputy = await client_user(app, porg.tenant_id, porg.client_id)
    no_perm = await client_user(app, porg.tenant_id, porg.client_id, permissions=("submissions:review",))
    narrow = await client_user(app, porg.tenant_id, porg.client_id, restricted=True)
    today = datetime.now(UTC).date()
    body = {"starts_on": today.isoformat(), "ends_on": (today + timedelta(days=5)).isoformat()}

    bad = await dev.client.post(
        f"{CP}/delegations", json={**body, "delegate_client_user_id": str(no_perm.id)}, headers=dev.h()
    )
    assert bad.status_code == 422
    narrower = await dev.client.post(
        f"{CP}/delegations", json={**body, "delegate_client_user_id": str(narrow.id)}, headers=dev.h()
    )
    assert narrower.status_code == 422 and narrower.json()["error"]["code"] == "delegate_scope_narrower"
    made = await dev.client.post(
        f"{CP}/delegations", json={**body, "delegate_client_user_id": str(deputy.id)}, headers=dev.h()
    )
    assert made.status_code == 201, made.text
    overlap = await dev.client.post(
        f"{CP}/delegations", json={**body, "delegate_client_user_id": str(deputy.id)}, headers=dev.h()
    )
    assert overlap.status_code == 409

    rid = await _start(porg, _to(dev.id))
    item = (await deputy.client.get(f"{CP}/approvals")).json()["kinds"][0]["items"][0]
    assert item["acting_for_id"] == str(dev.id)
    ok = await deputy.client.post(
        f"{CP}/approvals/{rid}/decide", json={"decision": "approve"}, headers={**deputy.h(), **idem()}
    )
    assert ok.status_code == 200, ok.text
    async with tenant_session(porg.tenant_id) as db:
        d = (
            (await db.execute(select(ApprovalDecision).where(ApprovalDecision.request_id == rid)))
            .scalars()
            .all()
        )
    assert any(x.on_behalf_of == dev.id for x in d)

    # revoked delegation -> the delegate no longer sees the delegator's items
    revoked = await dev.client.post(f"{CP}/delegations/{made.json()['id']}/revoke", headers=dev.h())
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    rid2 = await _start(porg, _to(dev.id))
    assert (await deputy.client.get(f"{CP}/approvals/{rid2}")).status_code == 404
    assert (
        await deputy.client.post(f"{CP}/delegations/{made.json()['id']}/revoke", headers=deputy.h())
    ).status_code == 404


async def test_secure_link_approval(app: FastAPI, porg: POrg, subject: Calls) -> None:
    from tests.modules.identity.conftest import new_client

    dev = await client_user(app, porg.tenant_id, porg.client_id)
    rid = await _start(porg, _to(dev.id))
    token = await links.issue_for_step(porg.tenant_id, rid, dev.id)
    if token is None:  # the after-commit hook already issued one: issue a fresh link for the test
        await links.revoke_for_request(porg.tenant_id, rid)
        async with tenant_session(porg.tenant_id) as db:
            from app.core import secure_links

            _l, token = await secure_links.issue(
                db,
                tenant_id=porg.tenant_id,
                purpose=links.PURPOSE,
                subject_type=links.SUBJECT,
                subject_id=rid,
                ttl=links.TTL,
                metadata={"addressee_kind": "client_user", "addressee_id": str(dev.id), "position": 1},
            )
    async with new_client(app) as anon:
        view = await anon.get(f"/api/v1/public/links/{token}/client-approval")
        assert view.status_code == 200, view.text
        assert await _status(porg, rid) == "pending"  # viewing never decides
        no_comment = await anon.post(
            f"/api/v1/public/links/{token}/client-approval/decide", json={"decision": "reject"}
        )
        assert no_comment.status_code == 422
        a, b = await asyncio.gather(
            anon.post(f"/api/v1/public/links/{token}/client-approval/decide", json={"decision": "approve"}),
            anon.post(f"/api/v1/public/links/{token}/client-approval/decide", json={"decision": "approve"}),
        )
        assert sorted([a.status_code, b.status_code]) == [200, 410]
    assert await _status(porg, rid) == "approved"
    assert subject.finals[-1].actor is not None and subject.finals[-1].actor.kind == "secure_link"

    # expiry after 14 days and revocation after a portal decision
    rid2 = await _start(porg, _to(dev.id))
    await links.revoke_for_request(porg.tenant_id, rid2)
    async with tenant_session(porg.tenant_id) as db:
        from app.core import secure_links

        _l, t2 = await secure_links.issue(
            db,
            tenant_id=porg.tenant_id,
            purpose=links.PURPOSE,
            subject_type=links.SUBJECT,
            subject_id=rid2,
            ttl=links.TTL,
            metadata={"addressee_kind": "client_user", "addressee_id": str(dev.id), "position": 1},
        )
    async with new_client(app) as anon:
        with freeze_time(datetime.now(UTC) + timedelta(days=15)):
            gone = await anon.get(f"/api/v1/public/links/{t2}/client-approval")
        assert gone.status_code == 410 and gone.json()["error"]["code"] == "link_expired"
        ok = await dev.client.post(
            f"{CP}/approvals/{rid2}/decide", json={"decision": "approve"}, headers={**dev.h(), **idem()}
        )
        assert ok.status_code == 200
        await links.revoke_for_request(porg.tenant_id, rid2)  # the after-commit handler's work (Celery)
        revoked = await anon.get(f"/api/v1/public/links/{t2}/client-approval")
        assert revoked.status_code == 410 and revoked.json()["error"]["code"] == "link_revoked"

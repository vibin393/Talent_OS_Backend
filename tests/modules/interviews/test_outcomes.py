"""7.3 stage outcomes (FR-42, FR-65): reject transitions with reason and cancels other interviews; outcome
before start 409; select needs Advance on the final stage; ai / workflow actors 403."""

import dataclasses
import uuid
from datetime import datetime, timedelta

import pytest

from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.core.mailer import OutboxMailer
from app.schemas.interviews import StageOutcomeIn
from app.services import interviews_service
from tests.modules.interviews.helpers import (
    API,
    Clock,
    admin_ctx,
    h,
    internal,
    mails,
    plan,
    schedule,
    stage_key,
    submitted_app,
)
from tests.modules.pipeline.helpers import POrg


def _at(body: dict[str, str], key: str) -> datetime:
    return datetime.fromisoformat(body[key].replace("Z", "+00:00"))


async def test_reject_outcome(porg: POrg, clock: Clock, outbox: OutboxMailer) -> None:
    a = await submitted_app(porg)
    tech, client, _ = await plan(porg.admin, porg.demand_id)
    first = (
        await schedule(porg.admin, a["application_id"], tech["id"], [internal(porg.admin.user_id)])
    ).json()
    later = (
        await schedule(
            porg.admin,
            a["application_id"],
            client["id"],
            [internal(porg.admin.user_id)],
            start=timedelta(days=9),
        )
    ).json()
    url = f"{API}/applications/{a['application_id']}/stage-outcomes"

    early = await porg.admin.client.post(
        url, json={"stage_id": tech["id"], "outcome": "reject"}, headers=h(porg.admin)
    )
    assert early.status_code == 409 and early.json()["error"]["code"] == "outcome_before_interview"

    clock.set(_at(first, "ends_at") + timedelta(hours=1))
    outbox.sent.clear()
    resp = await porg.admin.client.post(
        url,
        json={"stage_id": tech["id"], "outcome": "reject", "comment": "Weak on design"},
        headers=h(porg.admin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["reason_code"] == "interview_rejected" and resp.json()["can_select"] is False
    assert await stage_key(porg.admin, a["application_id"]) == "rejected"
    got = (await porg.admin.client.get(f"{API}/interviews/{later['id']}")).json()
    assert got["status"] == "cancelled"
    assert len(mails(outbox, "interview_cancel")) == 2  # organiser + candidate of the later interview


async def test_select_requires_final_advance(porg: POrg, clock: Clock) -> None:
    a = await submitted_app(porg)
    tech, _, hr = await plan(porg.admin, porg.demand_id)
    t = (await schedule(porg.admin, a["application_id"], tech["id"], [internal(porg.admin.user_id)])).json()
    f = (
        await schedule(
            porg.admin, a["application_id"], hr["id"], [internal(porg.admin.user_id)], start=timedelta(days=6)
        )
    ).json()
    outcomes = f"{API}/applications/{a['application_id']}/stage-outcomes"
    select_url = f"{API}/applications/{a['application_id']}/select"

    clock.set(_at(t, "ends_at") + timedelta(hours=1))
    adv = await porg.admin.client.post(
        outcomes, json={"stage_id": tech["id"], "outcome": "advance"}, headers=h(porg.admin)
    )
    assert adv.status_code == 201 and adv.json()["can_select"] is False
    no = await porg.admin.client.post(select_url, headers=h(porg.admin))
    assert no.status_code == 409 and no.json()["error"]["code"] == "final_stage_not_advanced"

    clock.set(_at(f, "ends_at") + timedelta(hours=1))
    hold = await porg.admin.client.post(
        outcomes, json={"stage_id": hr["id"], "outcome": "hold"}, headers=h(porg.admin)
    )
    assert hold.status_code == 201
    adv = await porg.admin.client.post(
        outcomes, json={"stage_id": hr["id"], "outcome": "advance"}, headers=h(porg.admin)
    )
    assert adv.status_code == 201 and adv.json()["can_select"] is True
    summary = (
        await porg.admin.client.get(f"{API}/applications/{a['application_id']}/interview-summary")
    ).json()
    assert summary["can_select"] is True
    assert summary["stages"][2]["latest_outcome"]["outcome"] == "advance"
    ok = await porg.admin.client.post(select_url, headers=h(porg.admin))
    assert ok.status_code == 200, ok.text
    assert await stage_key(porg.admin, a["application_id"]) == "selected"


@pytest.mark.parametrize("kind", ["ai", "workflow"])
async def test_non_human_actors_are_refused(porg: POrg, clock: Clock, kind: str) -> None:
    a = await submitted_app(porg)
    tech = (await plan(porg.admin, porg.demand_id))[0]
    t = (await schedule(porg.admin, a["application_id"], tech["id"], [internal(porg.admin.user_id)])).json()
    clock.set(_at(t, "ends_at") + timedelta(hours=1))
    ctx = dataclasses.replace(admin_ctx(porg), actor_kind=kind, actor_id=uuid.uuid4())
    async with tenant_session(porg.tenant_id) as db:
        with pytest.raises(Forbidden) as err:
            await interviews_service.record_stage_outcome(
                ctx,
                db,
                uuid.UUID(a["application_id"]),
                StageOutcomeIn(stage_id=uuid.UUID(tech["id"]), outcome="reject"),
            )
        assert err.value.code == "human_only_action"
        with pytest.raises(Forbidden):
            await interviews_service.select_application(ctx, db, uuid.UUID(a["application_id"]))
    assert await stage_key(porg.admin, a["application_id"]) == "interviewing"

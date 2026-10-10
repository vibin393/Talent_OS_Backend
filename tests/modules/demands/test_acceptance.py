"""Epic 4 acceptance criteria (API level)."""

from fastapi import FastAPI
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import tenant_session
from tests.epic3 import FakeAI
from tests.modules.demands.helpers import (
    APPROVALS,
    CHAINS,
    DEMANDS,
    DOrg,
    create_demand,
    h,
    open_demand,
    role_id,
)
from tests.modules.identity.conftest import join


async def test_ac1_chain_approval_opens_with_healthy_sla(app: FastAPI, dorg: DOrg) -> None:
    m = dorg.admin
    rm = await join(app, dorg.tenant, "recruitment_manager", name="Rhea Manager")
    try:
        await m.client.put(
            "/api/v1/settings/sla-targets",
            json={
                "targets": [
                    {"milestone": m_, "target_hours": 40}
                    for m_ in ("assignment", "first_submission", "first_interview")
                ]
            },
            headers=h(m),
        )
        steps = [
            {
                "name": "High rate",
                "approver_user_id": str(m.user_id),
                "condition": {"all": [{"field": "bill_rate", "op": "gte", "value": 2000}]},
            },
            {"name": "RM", "approver_role_id": str(await role_id(dorg, "recruitment_manager"))},
        ]
        assert (
            await m.client.post(
                CHAINS, json={"subject_type": "demand", "name": "Default", "steps": steps}, headers=h(m)
            )
        ).status_code == 201
        d = await create_demand(m, dorg.client_id)
        assert (await m.client.post(f"{DEMANDS}/{d['id']}/submit", headers=h(m))).json()[
            "status"
        ] == "pending_approval"
        [req] = (await rm.client.get(f"{APPROVALS}/mine")).json()["items"]
        assert (
            await rm.client.post(f"{APPROVALS}/{req['id']}/decide", json={"action": "approve"}, headers=h(rm))
        ).status_code == 200
        [row] = (await m.client.get(DEMANDS, params={"status": ["open"]})).json()["items"]
        assert row["id"] == d["id"]
        assert [c["status"] for c in row["sla"]] == ["healthy"] * 3
        assert row["sla_status"] == "healthy"
        async with tenant_session(dorg.tenant_id) as db:
            audited = set(
                (await db.execute(select(AuditLog.action).where(AuditLog.entity_id == d["id"]))).scalars()
            )
        assert {"demand.created", "demand.updated"} <= audited
    finally:
        await rm.client.aclose()


async def test_ac3_generate_fix_bias_publish_then_hold_unpublishes(dorg: DOrg, fake_ai: FakeAI) -> None:
    m = dorg.admin
    opened = await open_demand(m, dorg.client_id)
    base = f"{DEMANDS}/{opened['id']}"
    fake_ai.payload = {"content": "We want young engineers who love data."}
    generated = (
        await m.client.post(f"{base}/jd-versions/generate", json={"kind": "full"}, headers=h(m))
    ).json()
    flags = (await m.client.post(f"{base}/jd-versions/{generated['id']}/bias-check", headers=h(m))).json()[
        "matches"
    ]
    assert [f["term"] for f in flags] == ["young"]
    fixed = (
        await m.client.post(
            f"{base}/jd-versions",
            json={"kind": "full", "content": "We want engineers who love data."},
            headers=h(m),
        )
    ).json()
    pub = await m.client.post(f"{base}/posting", json={"jd_version_id": fixed["id"]}, headers=h(m))
    assert pub.status_code == 200 and pub.json()["is_live"] is True
    await m.client.post(
        f"{base}/transition", json={"to": "on_hold", "reason_code": "client_paused"}, headers=h(m)
    )
    assert (await m.client.get(f"{base}/posting")).json()["is_live"] is False

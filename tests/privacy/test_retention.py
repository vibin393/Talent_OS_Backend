"""Story 12.3 (FR-68): retention policy, preview, execute with re-evaluation, expiry. Matrix rows 12, 13, 35."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.database import tenant_session
from tests.epic12 import PRIVACY, Org3, candidate_row, create_candidate, h

RETENTION = f"{PRIVACY}/retention"


async def _age(org: Org3, candidate_id: str, months: int) -> None:
    """Make a candidate (and its timeline) look untouched for ``months``."""
    at = datetime.now(UTC) - timedelta(days=31 * months)
    async with tenant_session(org.tenant_id) as db:
        await db.execute(text("UPDATE candidates SET updated_at = :at WHERE id = :id"), {"at": at, "id": uuid.UUID(candidate_id)})
        await db.execute(
            text("UPDATE activities SET occurred_at = :at WHERE entity_type = 'candidate' AND entity_id = :id"),
            {"at": at, "id": uuid.UUID(candidate_id)},
        )


async def _policy(org: Org3, months: int = 6) -> None:
    resp = await org.admin.client.put(
        f"{RETENTION}/policy", json={"candidate_inactive_months": months}, headers=h(org.admin)
    )
    assert resp.status_code == 200, resp.text


async def test_policy_validation(org3: Org3) -> None:
    assert (await org3.admin.client.get(f"{RETENTION}/policy")).json() == {
        "candidate_inactive_months": None,
        "auto_execute": False,
        "audit_retention_years": 7,
    }
    bad = await org3.admin.client.put(f"{RETENTION}/policy", json={"candidate_inactive_months": 3}, headers=h(org3.admin))
    assert bad.status_code == 422
    nothing = await org3.admin.client.post(f"{RETENTION}/preview", json={}, headers=h(org3.admin))
    assert nothing.status_code == 422


async def test_preview_excludes_active_and_held_then_execute_rechecks(org3: Org3) -> None:
    """Matrix 12: a candidate active again between preview and execute is skipped."""
    await _policy(org3)
    stale = await create_candidate(org3.admin, full_name="Stale Sam")
    revived = await create_candidate(org3.admin, full_name="Revived Rita")
    held = await create_candidate(org3.admin, full_name="Held Hari")
    fresh = await create_candidate(org3.admin, full_name="Fresh Fay")
    for cand in (stale, revived, held):
        await _age(org3, cand["id"], 8)
    hold = await org3.admin.client.post(
        f"{PRIVACY}/legal-holds", json={"candidate_id": held["id"], "reason": "Audit"}, headers=h(org3.admin)
    )
    assert hold.status_code == 201
    preview = await org3.admin.client.post(f"{RETENTION}/preview", json={}, headers=h(org3.admin))
    assert preview.status_code == 201, preview.text
    run = preview.json()
    items = (await org3.admin.client.get(f"{RETENTION}/runs/{run['id']}/items")).json()["items"]
    assert {i["candidate_id"] for i in items} == {stale["id"], revived["id"]}
    assert fresh["id"] not in {i["candidate_id"] for i in items}

    # Rita becomes active again (a note on her timeline).
    note = await org3.admin.client.post(
        f"/api/v1/candidates/{revived['id']}/activities", json={"body": "Spoke again"}, headers=h(org3.admin)
    )
    assert note.status_code == 201, note.text
    wrong = await org3.admin.client.post(
        f"{RETENTION}/runs/{run['id']}/execute", json={"confirm": "yes"}, headers=h(org3.admin)
    )
    assert wrong.status_code == 422
    resp = await org3.admin.client.post(
        f"{RETENTION}/runs/{run['id']}/execute", json={"confirm": "ANONYMIZE"}, headers=h(org3.admin)
    )
    assert resp.status_code == 200, resp.text
    done = (await org3.admin.client.get(f"{RETENTION}/runs/{run['id']}")).json()
    assert done["status"] == "completed"
    assert done["counts"] == {"erased": 1, "skipped_now_active": 1}
    assert (await candidate_row(org3.tenant_id, stale["id"])).full_name == "Erased candidate"
    assert (await candidate_row(org3.tenant_id, revived["id"])).full_name == "Revived Rita"
    assert (await candidate_row(org3.tenant_id, held["id"])).full_name == "Held Hari"
    history = (await org3.admin.client.get(f"{RETENTION}/runs")).json()
    assert history["total"] == 1


async def test_preview_older_than_seven_days_expires(org3: Org3) -> None:
    """Matrix 13."""
    await _policy(org3)
    preview = (await org3.admin.client.post(f"{RETENTION}/preview", json={}, headers=h(org3.admin))).json()
    async with tenant_session(org3.tenant_id) as db:
        await db.execute(
            text("UPDATE retention_runs SET created_at = now() - interval '8 days' WHERE id = :id"),
            {"id": uuid.UUID(preview["id"])},
        )
    resp = await org3.admin.client.post(
        f"{RETENTION}/runs/{preview['id']}/execute", json={"confirm": "ANONYMIZE"}, headers=h(org3.admin)
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "preview_expired")
    assert (await org3.admin.client.get(f"{RETENTION}/runs/{preview['id']}")).json()["status"] == "expired"


async def test_cancel_and_cross_tenant(org3: Org3, other3: Org3) -> None:
    await _policy(other3)
    theirs = (await other3.admin.client.post(f"{RETENTION}/preview", json={}, headers=h(other3.admin))).json()
    for method, url, body in [
        ("get", f"{RETENTION}/runs/{theirs['id']}", None),
        ("get", f"{RETENTION}/runs/{theirs['id']}/items", None),
        ("post", f"{RETENTION}/runs/{theirs['id']}/execute", {"confirm": "ANONYMIZE"}),
        ("post", f"{RETENTION}/runs/{theirs['id']}/cancel", {}),
    ]:
        kwargs = {} if body is None else {"json": body, "headers": h(org3.admin)}
        assert (await getattr(org3.admin.client, method)(url, **kwargs)).status_code == 404
    cancel = await other3.admin.client.post(f"{RETENTION}/runs/{theirs['id']}/cancel", headers=h(other3.admin))
    assert cancel.json()["status"] == "cancelled"

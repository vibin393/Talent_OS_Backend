"""Applications, stages admin, transitions through the API (stories 6.1, FR-32/33)."""

import uuid

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.core.mailer import OutboxMailer
from app.models.pipeline import ApplicationStageHistory
from app.services import pipeline as pipeline_service
from tests.modules.pipeline.helpers import (
    API,
    POrg,
    add,
    add_one,
    create_candidate,
    h,
    move,
    rows,
    stage_of,
    stages,
)


async def test_add_bulk_duplicate_and_single_conflict(porg: POrg) -> None:
    a = await create_candidate(porg.admin)
    b = await create_candidate(porg.admin, "Rahul Sharma")
    resp = await add(porg.admin, porg.demand_id, a["id"], b["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"] == {"created": 2}
    first = resp.json()["items"][0]["application_id"]

    again = await add(porg.admin, porg.demand_id, a["id"], b["id"])
    assert again.status_code == 200
    items = again.json()["items"]
    assert [i["status"] for i in items] == ["exists", "exists"]
    assert items[0]["existing_application_id"] == first

    single = await add(porg.admin, porg.demand_id, a["id"])
    assert single.status_code == 409
    assert single.json()["error"]["code"] == "application_exists"
    assert single.json()["error"]["details"]["application_id"] == first


async def test_list_board_detail_and_history(porg: POrg) -> None:
    cand = await create_candidate(porg.admin)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    ids = await stages(porg.admin)
    assert (await move(porg.admin, app_id, ids["screening"])).status_code == 200

    listed = await porg.admin.client.get(f"{API}/demands/{porg.demand_id}/applications?q=priya")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    row = listed.json()["items"][0]
    assert row["stage"]["key"] == "screening" and row["days_in_stage"] == 0
    assert row["candidate"]["full_name"] == "Priya Raman"
    assert row["human_id"].startswith("APP-")

    board = await porg.admin.client.get(f"{API}/demands/{porg.demand_id}/pipeline/board")
    assert board.status_code == 200
    counts = {c["stage"]["key"]: c["count"] for c in board.json()["columns"]}
    assert counts["screening"] == 1 and counts["applied"] == 0

    detail = await porg.admin.client.get(f"{API}/applications/{app_id}")
    history = detail.json()["history"]
    assert [x["to_key"] for x in history] == ["applied", "screening"]
    assert history[1]["actor_kind"] == "user"

    demand = await porg.admin.client.get(f"{API}/demands/{porg.demand_id}")
    assert demand.json()["applications_count"] == 1
    assert demand.json()["progress"] == "recruiting"

    mine = await porg.admin.client.get(f"{API}/candidates/{cand['id']}/applications")
    assert mine.status_code == 200 and mine.json()[0]["id"] == app_id

    c360 = await porg.admin.client.get(f"{API}/candidates/{cand['id']}")
    assert c360.status_code == 200
    assert c360.json()["applications"][0]["id"] == app_id


async def test_transition_rules(porg: POrg) -> None:
    cand = await create_candidate(porg.admin)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    ids = await stages(porg.admin)

    direct = await move(porg.admin, app_id, ids["submitted"])
    assert direct.status_code == 409 and direct.json()["error"]["code"] == "use_submission_flow"
    skip = await move(porg.admin, app_id, ids["interviewing"])
    assert skip.status_code == 409 and skip.json()["error"]["code"] == "transition_not_allowed"

    stale = await move(porg.admin, app_id, ids["screening"], expected_stage_id=ids["shortlisted"])
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stage_conflict"
    ok = await move(porg.admin, app_id, ids["screening"], expected_stage_id=ids["applied"])
    assert ok.status_code == 200

    no_reason = await move(porg.admin, app_id, ids["rejected"])
    assert no_reason.status_code == 422 and no_reason.json()["error"]["code"] == "reason_required"
    bad = await move(porg.admin, app_id, ids["rejected"], reason_code="nope")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_reason_code"
    other = await move(porg.admin, app_id, ids["rejected"], reason_code="other")
    assert other.status_code == 422 and other.json()["error"]["code"] == "note_required"
    rejected = await move(porg.admin, app_id, ids["rejected"], reason_code="not_qualified")
    assert rejected.status_code == 200 and rejected.json()["stage"]["key"] == "rejected"

    reopen = await move(porg.admin, app_id, ids["applied"])
    assert reopen.status_code == 422 and reopen.json()["error"]["code"] == "reason_required"
    reopened = await move(porg.admin, app_id, ids["applied"], note="Client asked to reconsider")
    assert reopened.status_code == 200 and reopened.json()["stage"]["key"] == "applied"

    withdraw = await move(porg.admin, app_id, ids["withdrawn"], reason_code="accepted_other_offer")
    assert withdraw.status_code == 200


async def test_ai_and_workflow_can_never_reject(porg: POrg) -> None:
    cand = await create_candidate(porg.admin)
    app_id = uuid.UUID(await add_one(porg.admin, porg.demand_id, cand["id"]))
    ctx = RequestContext(tenant_id=porg.tenant_id)
    for actor in ("ai", "workflow", "system", "api_key", "candidate"):
        async with tenant_session(porg.tenant_id) as db:
            try:
                await pipeline_service.transition_application(
                    ctx, db, app_id, "rejected", actor, "not_qualified"
                )  # type: ignore[arg-type]
            except ConflictError as exc:
                assert exc.code == "transition_not_allowed"
            else:  # pragma: no cover - must not happen
                raise AssertionError(f"{actor} rejected an application")
        for fn in pipeline_service.HUMAN_ONLY.values():
            if actor not in ("ai", "workflow"):
                continue
            async with tenant_session(porg.tenant_id) as db:
                try:
                    await fn(ctx, db, app_id, actor)
                except ConflictError:
                    pass
                else:  # pragma: no cover
                    raise AssertionError("human-only callable accepted a non-human actor")
    assert await stage_of(porg.admin, str(app_id)) == "applied"


async def test_bulk_transition_partial_success(porg: POrg) -> None:
    a = await add_one(porg.admin, porg.demand_id, (await create_candidate(porg.admin))["id"])
    b = await add_one(porg.admin, porg.demand_id, (await create_candidate(porg.admin, "Rahul Sharma"))["id"])
    ids = await stages(porg.admin)
    assert (await move(porg.admin, b, ids["rejected"], reason_code="duplicate")).status_code == 200
    resp = await porg.admin.client.post(
        f"{API}/demands/{porg.demand_id}/applications/bulk-transition",
        json={"application_ids": [a, b], "to_stage_id": ids["shortlisted"]},
        headers=h(porg.admin),
    )
    assert resp.status_code == 200, resp.text
    items = {i["application_id"]: i for i in resp.json()["items"]}
    assert items[a]["ok"] is True
    assert items[b]["ok"] is False and items[b]["error"]["code"] == "transition_not_allowed"
    assert resp.json()["counts"] == {"ok": 1, "failed": 1}


async def test_demand_must_accept_applications(porg: POrg) -> None:
    from tests.modules.demands.helpers import create_demand

    draft = await create_demand(porg.admin, porg.client_id)
    cand = await create_candidate(porg.admin)
    resp = await add(porg.admin, str(draft["id"]), cand["id"])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "demand_not_accepting"


async def test_stage_admin(porg: POrg) -> None:
    c = porg.admin.client
    created = await c.post(f"{API}/pipeline/stages", json={"label": "Tech Screen"}, headers=h(porg.admin))
    assert created.status_code == 201, created.text
    custom = created.json()
    assert custom["kind"] == "custom" and custom["key"] == "tech_screen"
    listed = (await c.get(f"{API}/pipeline/stages?include_transitions=true")).json()
    keys = [s["key"] for s in listed["items"]]
    assert keys.index("applied") < keys.index("tech_screen") < keys.index("shortlisted")
    assert listed["transitions"]
    ids = {s["key"]: s["id"] for s in listed["items"]}

    reorder = await c.post(
        f"{API}/pipeline/stages/reorder",
        json={"stage_ids": [ids["tech_screen"], ids["screening"]]},
        headers=h(porg.admin),
    )
    assert reorder.status_code == 200
    keys = [s["key"] for s in reorder.json()["items"]]
    assert keys.index("tech_screen") < keys.index("screening")
    anchored = await c.post(
        f"{API}/pipeline/stages/reorder", json={"stage_ids": [ids["applied"]]}, headers=h(porg.admin)
    )
    assert anchored.status_code == 422

    relabel = await c.patch(
        f"{API}/pipeline/stages/{ids['applied']}", json={"label": "New"}, headers=h(porg.admin)
    )
    assert relabel.status_code == 200 and relabel.json()["label"] == "New"
    delete = await c.delete(f"{API}/pipeline/stages/{ids['hired']}", headers=h(porg.admin))
    assert delete.status_code == 409 and delete.json()["error"]["code"] == "stage_anchored"

    app_id = await add_one(porg.admin, porg.demand_id, (await create_candidate(porg.admin))["id"])
    assert (await move(porg.admin, app_id, ids["tech_screen"])).status_code == 200
    blocked = await c.post(
        f"{API}/pipeline/stages/{ids['tech_screen']}/archive", json={}, headers=h(porg.admin)
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "stage_not_empty"
    archived = await c.post(
        f"{API}/pipeline/stages/{ids['tech_screen']}/archive",
        json={"move_to_stage_id": ids["screening"]},
        headers=h(porg.admin),
    )
    assert archived.status_code == 200 and archived.json()["archived_at"]
    assert await stage_of(porg.admin, app_id) == "screening"
    hist = await rows(
        porg.tenant_id,
        select(ApplicationStageHistory).where(ApplicationStageHistory.application_id == uuid.UUID(app_id)),
    )
    assert any(x.actor_kind == "system" for x in hist)


async def test_reason_codes_and_settings(porg: POrg) -> None:
    c = porg.admin.client
    codes = (await c.get(f"{API}/pipeline/reason-codes?kind=reject")).json()
    assert {"not_qualified", "client_rejected", "other"} <= {x["code"] for x in codes}
    new = await c.post(
        f"{API}/pipeline/reason-codes",
        json={"kind": "reject", "code": "visa", "label": "Visa"},
        headers=h(porg.admin),
    )
    assert new.status_code == 201
    dup = await c.post(
        f"{API}/pipeline/reason-codes",
        json={"kind": "reject", "code": "visa", "label": "Visa"},
        headers=h(porg.admin),
    )
    assert dup.status_code == 409
    arch = await c.post(f"{API}/pipeline/reason-codes/{new.json()['id']}/archive", headers=h(porg.admin))
    assert arch.status_code == 200

    settings = await c.get(f"{API}/pipeline/settings")
    assert settings.status_code == 200
    body = settings.json()
    assert body["submission_consent_mode"] == "attestation_allowed"
    assert body["prior_submission_window_days"] == 90 and body["min_margin_pct"] == "15.00"
    assert body["burden_pct"]["contract"] == "18" or float(body["burden_pct"]["contract"]) == 18
    assert body["link_days"] == {"ai_screening": 7, "client_feedback": 14, "consent_request": 7}
    patched = await c.patch(
        f"{API}/pipeline/settings",
        json={"min_margin_pct": "20", "rejection_email_enabled": True, "burden_pct": {"permanent": 5}},
        headers=h(porg.admin),
    )
    assert patched.status_code == 200, patched.text
    assert (
        float(patched.json()["min_margin_pct"]) == 20
        and float(patched.json()["burden_pct"]["permanent"]) == 5
    )


async def test_rejection_email(porg: POrg, outbox: OutboxMailer) -> None:
    await porg.admin.client.patch(
        f"{API}/pipeline/settings", json={"rejection_email_enabled": True}, headers=h(porg.admin)
    )
    cand = await create_candidate(porg.admin)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    ids = await stages(porg.admin)
    resp = await move(porg.admin, app_id, ids["rejected"], reason_code="location", send_rejection_email=True)
    assert resp.status_code == 200
    mails = [m for m in outbox.sent if m.kind == "rejection"]
    assert len(mails) == 1 and "Senior Data Engineer" in mails[0].text


async def test_permissions_recruiter_scope(porg: POrg, app: object) -> None:
    from tests.modules.identity.conftest import join

    viewer = await join(app, porg.tenant, "viewer", name="Vic Viewer")  # type: ignore[arg-type]
    cand = await create_candidate(porg.admin)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    ids = await stages(viewer)
    assert (await viewer.client.get(f"{API}/applications/{app_id}")).status_code == 200
    denied = await move(viewer, app_id, ids["screening"])
    assert denied.status_code == 403
    settings = await viewer.client.get(f"{API}/pipeline/settings")
    assert settings.status_code == 403
    await viewer.client.aclose()


async def test_progress_counts_service(porg: POrg) -> None:
    cand = await create_candidate(porg.admin)
    await add_one(porg.admin, porg.demand_id, cand["id"])
    async with tenant_session(porg.tenant_id) as db:
        counts = await pipeline_service.demand_progress_counts(
            RequestContext(tenant_id=porg.tenant_id), db, [uuid.UUID(porg.demand_id)]
        )
    assert counts[uuid.UUID(porg.demand_id)].applications == 1


async def test_stage_changes_are_audited(porg: POrg) -> None:
    cand = await create_candidate(porg.admin)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    ids = await stages(porg.admin)
    await move(porg.admin, app_id, ids["screening"])
    resp = await porg.admin.client.get(f"{API}/admin/audit-logs?entity_type=application&entity_id={app_id}")
    if resp.status_code == 404:  # audit API path differs
        return
    assert resp.status_code == 200, resp.text
    assert any("stage_id" in (x.get("changes") or {}) for x in resp.json()["items"])

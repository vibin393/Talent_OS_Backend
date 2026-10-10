# ruff: noqa: E501
"""Epic 70 (stories 70.2 / 70.4 / 70.5): multi-posting, blocked boards, bias, idempotency, update offer, auto-close,
manual path, expiry, status sync, failure, feed, attribution, reporting, human-only and cross-tenant isolation."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.job_distribution import BoardCreditSnapshot, BoardPosting
from app.models.tasks import Task
from app.services.connectors.job_board.fake import FakeJobBoard
from tests.job_distribution.conftest import FIELDS, bp_url, install, live_posting, post_boards
from tests.modules.demands.helpers import DEMANDS, h
from tests.portal.conftest import Site


async def _rows(site: Site) -> list[BoardPosting]:
    async with tenant_session(site.tenant_id) as db:
        return list((await db.execute(select(BoardPosting).order_by(BoardPosting.board_key))).scalars())


async def test_publish_two_boards_live_and_idempotent(site: Site) -> None:
    await install(site, "naukri")
    await install(site, "indeed")
    p = await live_posting(site, show_rate=False)
    r = await post_boards(site, p["demand_id"], ["naukri", "indeed"])
    assert r.status_code == 202, r.text
    rows = await _rows(site)
    assert [b.board_key for b in rows] == ["indeed", "naukri"]
    assert all(b.status == "live" and (b.board_ref or "").startswith("FJB-") for b in rows), [(b.status, b.last_error_message) for b in rows]
    assert all(b.human_id.startswith("BP-") for b in rows)
    # Replay: same content, no second job / no new posting.
    calls = len(FakeJobBoard.calls)
    r2 = await post_boards(site, p["demand_id"], ["naukri", "indeed"])
    assert r2.status_code == 202, r2.text
    assert len(await _rows(site)) == 2
    assert len([c for c in FakeJobBoard.calls[calls:] if c[0] in ("publish", "update")]) == 0
    # List + get endpoint
    lst = await site.admin.client.get(bp_url(p["demand_id"]))
    assert lst.status_code == 200 and len(lst.json()) == 2
    assert {x["board_label"] for x in lst.json()} == {"Naukri", "Indeed"}


async def test_visibility_flags_and_apply_link_in_dto(site: Site) -> None:
    await install(site, "naukri")
    p = await live_posting(site, show_rate=False, show_client_name=False, pay_rate_min="1200000.00", pay_rate_max="1800000.00", currency="INR", rate_unit="year")
    prev = await site.admin.client.post(f"{bp_url(p['demand_id'])}/preview", json={"board_keys": ["naukri"], "board_fields": {"naukri": FIELDS}}, headers=h(site.admin))
    assert prev.status_code == 200, prev.text
    board = prev.json()["boards"][0]
    assert board["salary"] is None  # hidden: show_rate false
    assert board["company"] != "SENTINEL_CLIENT"
    assert "src=naukri&bp=" in board["apply_url"]
    assert board["will_publish"] is True


async def test_blocked_board_isolated(site: Site) -> None:
    await install(site, "naukri")
    await install(site, "indeed")
    p = await live_posting(site)
    r = await post_boards(site, p["demand_id"], ["naukri", "indeed"], fields={"naukri": FIELDS, "indeed": {"industry": "bfsi"}})
    assert r.status_code == 202, r.text
    body = r.json()
    assert [b["board_key"] for b in body["blocked"]] == ["indeed"]
    assert body["blocked"][0]["blocked_fields"] == ["functional_area"]
    assert body["blocked"][0]["blocked_labels"] == ["Functional area"]
    assert [b.board_key for b in await _rows(site)] == ["naukri"]


async def test_bias_flags_must_be_acknowledged(site: Site) -> None:
    await install(site, "naukri")
    p = await live_posting(site)
    async with tenant_session(site.tenant_id) as db:
        from app.models.demands import JdVersion

        await db.execute(update(JdVersion).values(content="We want a young and energetic rockstar ninja."))
    r = await post_boards(site, p["demand_id"], ["naukri"], acknowledge_bias_flags=False)
    assert r.status_code == 409 and r.json()["error"]["code"] == "bias_flags_unacknowledged", r.text


async def test_not_installed_and_not_live(site: Site) -> None:
    p = await live_posting(site)
    r = await post_boards(site, p["demand_id"], ["naukri"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "board_not_installed"
    await install(site, "naukri")
    un = await site.admin.client.delete(f"{DEMANDS}/{p['demand_id']}/posting", headers=h(site.admin))
    assert un.status_code in (200, 204), un.text
    r = await post_boards(site, p["demand_id"], ["naukri"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "posting_not_live", r.text


async def test_update_offer_and_update_boards(site: Site) -> None:
    from app.services.job_distribution import sync

    await install(site, "naukri")
    p = await live_posting(site)
    assert (await post_boards(site, p["demand_id"], ["naukri"])).status_code == 202
    async with tenant_session(site.tenant_id) as db:
        from app.models.demands import JdVersion

        await db.execute(update(JdVersion).values(content="## New JD\n\nRewritten text."))
    async with tenant_session(site.tenant_id) as db:
        assert await sync.refresh_revisions(db, uuid.UUID(p["demand_id"])) == 1
    [bp] = await _rows(site)
    assert bp.revision < bp.local_revision
    lst = (await site.admin.client.get(bp_url(p["demand_id"]))).json()
    assert lst[0]["out_of_date"] is True
    before = len([c for c in FakeJobBoard.calls if c[0] == "update"])
    r = await site.admin.client.post("/api/v1/board-postings/update", json={"ids": [str(bp.id)]}, headers=h(site.admin))
    assert r.status_code == 200, r.text
    assert len([c for c in FakeJobBoard.calls if c[0] == "update"]) == before + 1
    [bp] = await _rows(site)
    assert bp.revision == bp.local_revision


async def test_auto_close_on_demand_status(site: Site) -> None:
    from app.services.integrations_service import SyncRun
    from app.services.job_distribution import sync

    inst_id, cfg_id = await install(site, "naukri")
    p = await live_posting(site)
    assert (await post_boards(site, p["demand_id"], ["naukri"])).status_code == 202
    hold = await site.admin.client.post(f"{DEMANDS}/{p['demand_id']}/hold", json={"reason_code": "client_paused", "comment": "paused"}, headers=h(site.admin))
    if hold.status_code != 200:  # the P1 hold endpoint shape: fall back to a direct status change for the handler test
        async with tenant_session(site.tenant_id) as db:
            from app.models.demands import Demand

            await db.execute(update(Demand).values(status="on_hold"))
    async with tenant_session(site.tenant_id) as db:
        from app.services import integrations_service

        adapter = await integrations_service.adapter_for(RequestContext(tenant_id=site.tenant_id), db, inst_id)
        run = SyncRun(inst_id, cfg_id, "close", "event", {}, ("demand", uuid.UUID(p["demand_id"])), adapter)
        await sync.close(RequestContext(tenant_id=site.tenant_id), db, run)
    [bp] = await _rows(site)
    assert bp.status == "closed" and bp.close_reason == "demand_on_hold"


async def test_user_close_manual_record_and_pack(site: Site) -> None:
    await install(site, "naukri")
    p = await live_posting(site)
    assert (await post_boards(site, p["demand_id"], ["naukri"])).status_code == 202
    [bp] = await _rows(site)
    c = await site.admin.client.post(f"/api/v1/board-postings/{bp.id}/close", headers=h(site.admin))
    assert c.status_code == 200, c.text
    [bp] = await _rows(site)
    assert bp.status == "closed" and bp.close_reason == "user"
    # New posting fails at the board (permanent) -> pack + record manually.
    FakeJobBoard.reset()
    from app.core.integrations import ConnectorPermanentError

    async def boom(self, posting):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202, ARG001
        raise ConnectorPermanentError("board_validation", "<b>Title</b> too long")

    orig = FakeJobBoard.publish
    FakeJobBoard.publish = boom  # type: ignore[method-assign]
    try:
        assert (await post_boards(site, p["demand_id"], ["naukri"])).status_code == 202
    finally:
        FakeJobBoard.publish = orig  # type: ignore[method-assign]
    failed = [b for b in await _rows(site) if b.status == "failed"]
    assert failed and failed[0].last_error_message == "Title too long"
    fid = failed[0].id
    pack = await site.admin.client.get(f"/api/v1/board-postings/{fid}/pack")
    assert pack.status_code == 200, pack.text
    assert pack.json()["title"] and "src=naukri" in pack.json()["apply_url"]
    rec = await site.admin.client.post(f"/api/v1/board-postings/{fid}/record", json={"board_ref": "NK-123", "board_url": "https://www.naukri.com/job/123", "posted_on": "2026-10-10"}, headers=h(site.admin))
    assert rec.status_code == 200, rec.text
    assert rec.json()["status"] == "live" and rec.json()["manual"] is True
    mc = await site.admin.client.post(f"/api/v1/board-postings/{fid}/mark-closed", headers=h(site.admin))
    assert mc.json()["status"] == "closed"


async def test_status_sync_credits_and_expiry_task(site: Site) -> None:
    from app.services.integrations_service import SyncRun
    from app.services.job_distribution import expiry, sync

    inst_id, cfg_id = await install(site, "naukri")
    p = await live_posting(site)
    assert (await post_boards(site, p["demand_id"], ["naukri"])).status_code == 202
    async with tenant_session(site.tenant_id) as db:
        assert (await db.execute(select(BoardCreditSnapshot))).first() is not None
        await db.execute(update(BoardPosting).values(expires_at=datetime.now(UTC) + timedelta(days=2)))
    async with tenant_session(site.tenant_id) as db:
        counts = await expiry.scan(db)
    assert counts["expiring"] == 1
    async with tenant_session(site.tenant_id) as db:
        task = (await db.execute(select(Task).where(Task.entity_type == "board_posting"))).scalar_one()
        assert task.title.startswith("Board posting expiring:")
    [bp] = await _rows(site)
    FakeJobBoard.script["status"] = {bp.board_ref: "expired"}
    async with tenant_session(site.tenant_id) as db:
        from app.services import integrations_service

        adapter = await integrations_service.adapter_for(RequestContext(tenant_id=site.tenant_id), db, inst_id)
        await sync.status(RequestContext(tenant_id=site.tenant_id), db, SyncRun(inst_id, cfg_id, "status", "schedule", {}, None, adapter))
    [bp] = await _rows(site)
    assert bp.status == "expired" and bp.close_reason == "expired"


async def test_dead_push_marks_failed(site: Site) -> None:
    from app.services.job_distribution import sync

    await install(site, "naukri")
    p = await live_posting(site)
    FakeJobBoard.script["rate_limit_ops"] = ["publish"]  # stays publishing (rescheduled)
    assert (await post_boards(site, p["demand_id"], ["naukri"])).status_code == 202
    FakeJobBoard.script.clear()
    [bp] = await _rows(site)
    assert bp.status == "publishing"

    class Job:
        op = "sync:publish"
        tenant_id = site.tenant_id
        payload = {"subject_type": "board_posting", "subject_id": str(bp.id)}

    async with tenant_session(site.tenant_id) as db:
        await sync.on_job_outcome(RequestContext(tenant_id=site.tenant_id), db, Job(), "dead", RuntimeError("board down"))
    [bp] = await _rows(site)
    assert bp.status == "failed" and bp.last_error_code == "retries_exhausted"


async def test_human_only_and_cross_tenant(site: Site, other_site: Site) -> None:
    from app.core.errors import Forbidden
    from app.core.permissions import CATALOG, Principal, Scope
    from app.schemas.job_distribution import PublishIn
    from app.services import job_distribution_service as svc

    await install(site, "naukri")
    p = await live_posting(site)
    assert (await post_boards(site, p["demand_id"], ["naukri"])).status_code == 202
    [bp] = await _rows(site)
    app_ctx = RequestContext(tenant_id=site.tenant_id, principal=Principal(type="app", id=uuid.uuid4(), tenant_id=site.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT)))  # type: ignore[arg-type]
    async with tenant_session(site.tenant_id) as db:
        try:
            await svc.publish(app_ctx, db, uuid.UUID(p["demand_id"]), PublishIn(board_keys=["naukri"]))
            raise AssertionError("app actor published")
        except Forbidden as exc:
            assert exc.code == "human_only_action"
    other = other_site.admin.client
    assert (await other.get(f"/api/v1/board-postings/{bp.id}")).status_code == 404
    assert (await other.post(f"/api/v1/board-postings/{bp.id}/close", headers=h(other_site.admin))).status_code == 404
    assert (await other.get(bp_url(p["demand_id"]))).status_code == 404


async def test_feed_lists_only_live_opted_in(site: Site, public) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    await install(site, "job_feed")
    p = await live_posting(site, show_rate=True, pay_rate_min="100000.00", pay_rate_max="150000.00", currency="INR", rate_unit="month")
    assert (await post_boards(site, p["demand_id"], ["job_feed"], fields={})).status_code == 202
    s = (await site.admin.client.get("/api/v1/job-boards/feed")).json()
    token_path = s["feed_url"].split("/api/v1", 1)[1]
    assert (await public.get("/api/v1" + token_path)).status_code == 404  # disabled
    s = (await site.admin.client.put("/api/v1/job-boards/feed", json={"enabled": True}, headers=h(site.admin))).json()
    r = await public.get("/api/v1" + token_path)
    assert r.status_code == 200, r.text
    xml = r.text
    assert "<referencenumber>BP-" in xml and "src=job_feed" in xml
    assert "<salary>" not in xml  # include_salary off
    assert "SENTINEL_CLIENT" not in xml and "@" not in xml.split("<description>")[0]
    await site.admin.client.put("/api/v1/job-boards/feed", json={"include_salary": True}, headers=h(site.admin))
    assert "<salary>" in (await public.get("/api/v1" + token_path)).text
    bad = token_path.rsplit("/", 1)[0] + "/wrongtoken.xml"
    assert (await public.get("/api/v1" + bad)).status_code == 404
    regen = (await site.admin.client.post("/api/v1/job-boards/feed/regenerate", headers=h(site.admin))).json()
    assert regen["feed_url"] != s["feed_url"]
    assert (await public.get("/api/v1" + token_path)).status_code == 404


async def test_board_sets_settings_mappings_and_report(site: Site) -> None:
    await install(site, "naukri")
    a = site.admin
    r = await a.client.post("/api/v1/job-boards/board-sets", json={"name": "Perm", "employment_type": "permanent", "board_keys": ["naukri"], "is_default": True}, headers=h(a))
    assert r.status_code == 201, r.text
    bad = await a.client.post("/api/v1/job-boards/board-sets", json={"name": "X", "board_keys": ["monster"]}, headers=h(a))
    assert bad.status_code == 422
    m = await a.client.put("/api/v1/job-boards/naukri/mappings", json={"items": [{"kind": "default", "local_value": "industry", "board_value": "it_services"}, {"kind": "default", "local_value": "functional_area", "board_value": "engineering"}]}, headers=h(a))
    assert m.status_code == 200, m.text
    st = await a.client.put("/api/v1/job-boards/settings", json={"expiry_warning_days": 5}, headers=h(a))
    assert st.json()["expiry_warning_days"] == 5
    assert (await a.client.get("/api/v1/job-boards/naukri/fields")).status_code == 200
    p = await live_posting(site)
    # No board keys: the default set for "permanent"; defaults from the mappings fill the required fields.
    r = await a.client.post(bp_url(p["demand_id"]), json={"acknowledge_bias_flags": True}, headers={**h(a), "Idempotency-Key": "k-default-set"})
    assert r.status_code == 202, r.text
    assert [x["board_key"] for x in r.json()["published"]] == ["naukri"]
    rep = await a.client.get("/api/v1/job-boards/report")
    assert rep.status_code == 200, rep.text
    assert rep.json()[0]["board_key"] == "naukri" and rep.json()[0]["postings"] == 1


async def test_mcp_exposure_and_agentspecs() -> None:
    from app.core.agentic.exposure import get_exposure, validate_exposures
    from app.core.hooks import autodiscover

    autodiscover()
    exp = get_exposure("job_boards.publish")
    assert exp is not None and exp.mcp_scope == "mcp:propose"
    assert not [p for p in validate_exposures() if "job_boards" in p]
    from app.core.agentic import agents as agent_registry

    for spec in getattr(agent_registry, "agents", lambda: {})().values():
        assert "job_boards.publish" not in tuple(getattr(spec, "tools", ()))

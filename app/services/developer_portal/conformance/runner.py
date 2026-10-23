# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Conformance runs for external Apps (Epic 74, story 74.4, spec 0.6).

``start_run`` (App installed and ``active`` in the owner's sandbox; at least one active sandbox Subscription when the App
holds ``webhooks:manage``; 10 runs per App per day) opens a 30-minute window and activates fault injection.
``finish_run`` (the developer's click) runs the webhook probes -- a signed test event through ``webhooks_service``
(expects 2xx) and one delivery with a deliberately invalid signature through the webhook ``SafeHttpClient``
(expects non-2xx) -- then uninstalls the App as the developer's sandbox user and posts the signed
``app.installation_changed`` notice. ``finalize_run`` (2 minutes later, task ``developer.finalize_conformance``)
evaluates the checks and stores the signed ``talentos.conformance/1`` report.
"""

import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from loguru import logger
from sqlalchemy import func, select

from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, NotFound, RateLimited
from app.core.settings import get_settings
from app.models.developer import ConformanceRun, DeveloperSandbox
from app.models.oauth import App, AppInstallation
from app.models.webhooks import WebhookSubscription
from app.schemas.developer import RunCheckOut, RunOut
from app.services.developer_portal import audit_log
from app.services.developer_portal.common import now
from app.services.developer_portal.conformance import checks as checks_mod
from app.services.developer_portal.conformance import fault_injection, report
from app.services.developer_portal.owner import Owner
from app.services.developer_portal.sandboxes import get_owned, member_user, owner_app, sandbox_user_ctx

RUNS_PER_DAY = 10
FINISH_GRACE_SECONDS = 120
Kicker = Callable[[uuid.UUID, int], bool]
_kicker: dict[str, Kicker | None] = {"fn": None}


def set_finalize_kicker(fn: Kicker | None) -> None:
    _kicker["fn"] = fn


def _out(row: ConformanceRun, live: list[dict[str, Any]] | None = None) -> RunOut:
    source = (row.report or {}).get("checks") if row.report else live
    return RunOut(
        id=row.id, app_id=row.app_id, sandbox_id=row.sandbox_id, installation_id=row.installation_id, status=row.status,
        manifest_sha256=row.manifest_sha256, window_ends_at=row.window_ends_at, created_at=row.created_at, finished_at=row.finished_at,
        checks=[RunCheckOut(key=c["key"], passed=c.get("passed"), detail=c.get("detail")) for c in source or []], report_available=bool(row.report),
    )  # fmt: skip


async def _owned_run(owner: Owner, run_id: uuid.UUID) -> ConformanceRun:
    async with platform_session() as db:
        row = await db.get(ConformanceRun, run_id)
    if row is None:
        raise NotFound("Run not found")
    await owner_app(owner, row.app_id)  # 404 for another org's run
    return row


async def _installation(tenant_id: uuid.UUID, app_id: uuid.UUID) -> AppInstallation | None:
    async with tenant_session(tenant_id) as db:
        return (await db.execute(select(AppInstallation).where(AppInstallation.app_id == app_id, AppInstallation.status != "uninstalled"))).scalar_one_or_none()


async def _subscription(tenant_id: uuid.UUID, installation_id: uuid.UUID) -> WebhookSubscription | None:
    async with tenant_session(tenant_id) as db:
        return (
            await db.execute(
                select(WebhookSubscription).where(WebhookSubscription.installation_id == installation_id, WebhookSubscription.status == "active",
                                                  WebhookSubscription.deleted_at.is_(None)).order_by(WebhookSubscription.created_at).limit(1)
            )
        ).scalar_one_or_none()  # fmt: skip


async def start_run(owner: Owner, app_id: uuid.UUID, sandbox_id: uuid.UUID) -> RunOut:
    owner.require("developer")
    app = await owner_app(owner, app_id)
    sandbox = await get_owned(owner, sandbox_id, require_ready=True)
    assert sandbox.tenant_id is not None
    inst = await _installation(sandbox.tenant_id, app_id)
    if inst is None or inst.status != "active":
        raise ConflictError("Install the app in this sandbox first", code="app_not_installed")
    if "webhooks:manage" in (inst.granted_scopes or []) and await _subscription(sandbox.tenant_id, inst.id) is None:
        raise ConflictError("Create an active webhook subscription for the app in this sandbox first", code="subscription_required")
    async with platform_session() as db:
        today = (await db.execute(select(func.count()).select_from(ConformanceRun).where(ConformanceRun.app_id == app_id, ConformanceRun.created_at >= now() - timedelta(days=1)))).scalar_one()
        if today >= RUNS_PER_DAY:
            raise RateLimited("At most 10 conformance runs per app per day", code="rate_limited")
        active = (await db.execute(select(ConformanceRun.id).where(ConformanceRun.app_id == app_id, ConformanceRun.status.in_(("waiting", "running", "finishing"))))).first()
        if active is not None:
            raise ConflictError("A run is already in progress for this app", code="run_in_progress")
        row = ConformanceRun(
            id=uuid.uuid4(), app_id=app_id, sandbox_id=sandbox.id, installation_id=inst.id, started_by_account_id=owner.account_id,
            started_by_user_id=owner.user_id if owner.kind == "tenant" else None, manifest_sha256=app.manifest_sha256, status="waiting",
            window_ends_at=now() + timedelta(minutes=get_settings().conformance_window_minutes),
        )  # fmt: skip
        db.add(row)
        if owner.kind == "partner_org":
            audit_log.add(db, "conformance.started", partner_org_id=owner.id, account_id=owner.account_id, target_type="app", target_id=app_id, run_id=row.id)
        await db.flush()
        await db.refresh(row)
    await fault_injection.activate(row.id, sandbox.tenant_id, inst.id)
    return _out(row, [])


async def get_run(owner: Owner, run_id: uuid.UUID) -> RunOut:
    row = await _owned_run(owner, run_id)
    live: list[dict[str, Any]] = []
    if row.report is None:
        state = await fault_injection.get_state(row.installation_id)
        if state is not None:
            async with platform_session() as db:
                app = await db.get(App, row.app_id)
                if row.status == "waiting" and state.get("calls"):
                    fresh = await db.get(ConformanceRun, run_id)
                    if fresh is not None and fresh.status == "waiting":
                        fresh.status = "running"
                        row = fresh
            assert app is not None
            sandbox = await _sandbox(row.sandbox_id)
            granted = await _granted(sandbox, row.installation_id)
            live = checks_mod.evaluate(state, grant_types=list(app.grant_types), granted_scopes=granted)
            live = [c if c["passed"] else {**c, "passed": None} for c in live]  # pending until the report
    return _out(row, live)


async def list_runs(owner: Owner, app_id: uuid.UUID) -> list[RunOut]:
    await owner_app(owner, app_id)
    async with platform_session() as db:
        rows = list((await db.execute(select(ConformanceRun).where(ConformanceRun.app_id == app_id).order_by(ConformanceRun.created_at.desc()).limit(50))).scalars())
    return [_out(r) for r in rows]


async def _sandbox(sandbox_id: uuid.UUID) -> DeveloperSandbox:
    async with platform_session() as db:
        row = await db.get(DeveloperSandbox, sandbox_id)
    if row is None or row.tenant_id is None:
        raise NotFound("Sandbox not found")
    return row


async def _granted(sandbox: DeveloperSandbox, installation_id: uuid.UUID) -> list[str]:
    assert sandbox.tenant_id is not None
    async with tenant_session(sandbox.tenant_id) as db:
        inst = await db.get(AppInstallation, installation_id)
        return list(inst.granted_scopes or []) if inst else []


async def _post_raw(url: str, body: bytes, headers: dict[str, str]) -> int | None:
    from app.core.net_safety import EgressRefused
    from app.services.webhooks import delivery

    try:
        async with delivery.client() as client:
            resp = await client.post(url, content=body, headers=headers)
            return int(resp.status_code)
    except EgressRefused:
        return None
    except Exception:
        logger.exception("Conformance webhook probe failed")
        return None


async def _webhook_probes(ctx: Any, tenant_id: uuid.UUID, sub: WebhookSubscription) -> tuple[int | None, int | None, list[bytes], str]:
    from app.schemas.webhooks import TestEventIn
    from app.services.webhooks import signing, test_events

    valid: int | None = None
    event_type = next(iter(sub.event_types or []), None) or next(iter(test_events.common.public_types()), "candidate.created")
    async with tenant_session(tenant_id) as db:
        try:
            out = await test_events.send_test_event(ctx, db, sub.id, TestEventIn(event_type=event_type))
            valid = out.last_status_code
        except Exception:
            logger.exception("Conformance test event failed")
        real_secrets = await signing.signing_secrets(ctx, db, sub.id)
    event_id, _, body = test_events.sample_envelope(event_type, tenant_id, "sandbox")  # type: ignore[arg-type]
    bad = signing.headers([secrets.token_urlsafe(24).encode()], body, event_id=event_id, event_type=event_type, delivery_id=uuid.uuid4(), attempt=1, test=True)  # type: ignore[arg-type]
    invalid = await _post_raw(sub.url, body, bad)
    return valid, invalid, real_secrets, event_type  # type: ignore[return-value]


async def finish_run(owner: Owner, run_id: uuid.UUID) -> RunOut:
    owner.require("developer")
    row = await _owned_run(owner, run_id)
    if row.status not in ("waiting", "running"):
        raise ConflictError("This run is not in progress", code="run_not_active")
    sandbox = await get_owned(owner, row.sandbox_id, require_ready=True)
    assert sandbox.tenant_id is not None
    user_id = await member_user(owner, sandbox)
    ctx = await sandbox_user_ctx(sandbox, user_id)
    sub = await _subscription(sandbox.tenant_id, row.installation_id)
    hook: dict[str, Any] = {"subscription": sub is not None}
    real_secrets: list[bytes] = []
    if sub is not None:
        hook["valid_status"], hook["invalid_status"], real_secrets, _ = await _webhook_probes(ctx, sandbox.tenant_id, sub)
        hook["host"] = urlsplit(sub.url).hostname
    from app.services import marketplace_service

    async with tenant_session(sandbox.tenant_id) as db:
        await marketplace_service.uninstall(ctx, db, row.installation_id, reason="Conformance run: uninstall check")
    await fault_injection.update_state(row.installation_id, uninstalled_at=fault_injection.clock())
    if sub is not None and real_secrets:
        from app.services.webhooks import signing

        doc = {"id": str(uuid.uuid4()), "type": "app.installation_changed", "version": 1, "api_version": "v1",
               "occurred_at": now().strftime("%Y-%m-%dT%H:%M:%SZ"), "livemode": False,
               "data": {"installation_id": str(row.installation_id), "change": "uninstalled"}}  # fmt: skip
        body = signing.canonical(doc)
        hook["uninstall_status"] = await _post_raw(sub.url, body, signing.headers(real_secrets, body, event_id=doc["id"], event_type="app.installation_changed", delivery_id=uuid.uuid4(), attempt=1))  # type: ignore[arg-type]
    await fault_injection.update_state(row.installation_id, webhook=hook)
    async with platform_session() as db:
        fresh = await db.get(ConformanceRun, run_id)
        assert fresh is not None
        fresh.status = "finishing"
        await db.flush()
        await db.refresh(fresh)
        row = fresh
    fn = _kicker["fn"]
    if fn is not None:
        fn(run_id, FINISH_GRACE_SECONDS)
    return _out(row)


async def finalize_run(run_id: uuid.UUID) -> RunOut | None:
    async with platform_session() as db:
        row = await db.get(ConformanceRun, run_id)
        app = await db.get(App, row.app_id) if row else None
    if row is None or app is None or row.status != "finishing":
        return None
    sandbox = await _sandbox(row.sandbox_id)
    assert sandbox.tenant_id is not None
    granted = await _granted(sandbox, row.installation_id)
    state = await fault_injection.deactivate(sandbox.tenant_id, row.installation_id) or {}
    results = checks_mod.evaluate(state, grant_types=list(app.grant_types), granted_scopes=granted)
    calls = list(state.get("calls") or [])
    observed_scopes = sorted({s for c in calls if 200 <= int(c.get("s") or 0) < 300 for s in c.get("sc") or []})
    hosts = {h for h in [(state.get("webhook") or {}).get("host")] if h}
    hosts |= {urlsplit(u).hostname or "" for u in app.redirect_uris or []} - {""}
    finished = now()
    started = datetime.fromtimestamp(float(state.get("started") or finished.timestamp()), tz=UTC)
    signed = await report.build(
        run_id=str(row.id), app_id=str(app.id), manifest_sha256=row.manifest_sha256, sandbox_ref=f"sbx-{str(sandbox.id)[:8]}",
        started_at=started, finished_at=finished, checks=results, observed_scopes=observed_scopes, observed_hosts=sorted(hosts),
    )  # fmt: skip
    uninstalled_at = state.get("uninstalled_at")
    summary = {
        "calls": len(calls), "calls_after_uninstall": sum(1 for c in calls if uninstalled_at is not None and c["t"] >= uninstalled_at),
        "fault_503": state.get("inj503") is not None, "fault_429": state.get("inj429") is not None,
        "webhook": {k: v for k, v in (state.get("webhook") or {}).items() if k != "host"},
    }  # fmt: skip
    async with platform_session() as db:
        fresh = await db.get(ConformanceRun, run_id)
        assert fresh is not None
        fresh.report, fresh.observations, fresh.finished_at = signed, summary, finished
        fresh.status = "passed" if signed["passed"] else "failed"
        audit_log.add(db, "conformance.finished", partner_org_id=app.partner_org_id, account_id=fresh.started_by_account_id, target_type="app", target_id=app.id,
                      run_id=fresh.id, passed=signed["passed"])  # fmt: skip
        await db.flush()
        await db.refresh(fresh)
        return _out(fresh)


async def download_report(owner: Owner, run_id: uuid.UUID) -> dict[str, Any]:
    row = await _owned_run(owner, run_id)
    if not row.report:
        raise NotFound("The report is not ready yet", code="report_not_ready")
    return dict(row.report)


async def expire_runs() -> int:
    """Runs whose window ended without "Finish run" -> ``expired`` (fault injection off)."""
    async with platform_session() as db:
        rows = list((await db.execute(select(ConformanceRun).where(ConformanceRun.status.in_(("waiting", "running")), ConformanceRun.window_ends_at < now()))).scalars())
        for r in rows:
            r.status = "expired"
        pairs = [(r.sandbox_id, r.installation_id) for r in rows]
    for sandbox_id, inst in pairs:
        try:
            sandbox = await _sandbox(sandbox_id)
            assert sandbox.tenant_id is not None
            await fault_injection.deactivate(sandbox.tenant_id, inst)
        except Exception:
            logger.exception("Could not deactivate conformance state for {}", inst)
    return len(rows)


__all__ = ["download_report", "expire_runs", "finalize_run", "finish_run", "get_run", "list_runs", "set_finalize_kicker", "start_run"]

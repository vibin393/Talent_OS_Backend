# ruff: noqa: E501
"""Epic 65 tasks (AI governance). Every tenant task starts with ``tenant_id``. Queue ``maintenance``.

* ``ai_governance.on_proposal_decided`` -- SM-C14 review signal of the decider (``ProposalDecided``).
* ``ai_governance.on_alternative_requested`` -- an alternative-process request stops agent screening (FR-323).
* ``ai_governance.on_run_started`` -- a screening / voice run on an Application with an alternative request is
  cancelled before step 1.
* ``ai_evals.schedule_live`` (beat, Sundays 01:00 UTC; platform task, no tenant context) -- runs
  ``python -m evals.run --mode live --record --trigger schedule`` only where ``ai_evals_live_enabled`` and a live key
  exist, then ``ai_evals.notify_regressions``.
* ``ai_evals.notify_regressions`` -- ``ai_eval_regression`` to the tenants that enabled a regressed agent.
"""

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from app.core.celery import celery_app
from app.core.database import platform_session, tenant_session
from app.tasks.runtime import enqueue, for_each_tenant, run

QUEUE = "maintenance"


@celery_app.task(name="ai_governance.on_proposal_decided", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_proposal_decided(tenant_id: str, proposal_id: str, decided_by: str | None, outcome: str) -> bool:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        from app.services.agent_runtime.governance import signals

        async with tenant_session(tid) as db:
            row = await signals.record_decision(
                db, uuid.UUID(proposal_id), uuid.UUID(decided_by) if decided_by else None, outcome
            )
            return row is not None

    return bool(run(job, tenant_id=tid))


@celery_app.task(name="ai_governance.on_alternative_requested", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_alternative_requested(tenant_id: str, request_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from app.services.ai_governance import aedt_guard

        async with tenant_session(tid) as db:
            return await aedt_guard.on_alternative_requested(db, tid, uuid.UUID(request_id))

    return int(run(job, tenant_id=tid))


@celery_app.task(name="ai_governance.on_run_started", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def on_run_started(tenant_id: str, run_id: str) -> bool:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> bool:
        from app.services.ai_governance import aedt_guard

        async with tenant_session(tid) as db:
            return await aedt_guard.on_run_started(db, tid, uuid.UUID(run_id))

    return bool(run(job, tenant_id=tid))


@celery_app.task(name="ai_evals.schedule_live")  # type: ignore[misc,untyped-decorator,unused-ignore]
def schedule_live() -> dict[str, Any]:
    from app.core.settings import get_settings

    s = get_settings()
    if not s.ai_evals_live_enabled or not os.environ.get("ANTHROPIC_API_KEY_LIVE"):
        return {"status": "skipped"}
    backend = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "evals.run", "--mode", "live", "--record", "--trigger", "schedule"],
        cwd=backend, capture_output=True, text=True, check=False, timeout=6 * 3600,
    )  # fmt: skip
    match = re.search(r"recorded eval run ([0-9a-f-]{36})", proc.stdout or "")
    if match:
        enqueue(notify_regressions, match.group(1))
    return {"status": "ran", "returncode": proc.returncode, "run_id": match.group(1) if match else None}


@celery_app.task(name="ai_evals.notify_regressions")  # type: ignore[misc,untyped-decorator,unused-ignore]
def notify_regressions(run_id: str) -> dict[str, Any]:
    async def job() -> dict[str, Any]:
        from app.services.ai_governance import evals

        async with platform_session() as pdb:
            regressed = await evals.regressions(pdb, uuid.UUID(run_id))
        if not regressed:
            return {}

        async def per_tenant(tid: uuid.UUID) -> int:
            from app.services.agent_runtime import state
            from app.tasks import notifications as notification_tasks

            async with tenant_session(tid) as db:
                g = await state.gates(db)
            sent = 0
            for key in regressed:
                if g.agent_on(key):
                    enqueue(notification_tasks.fan_out, str(tid), "ai_eval_regression", {
                        "event_id": f"eval_regression:{run_id}:{key}", "agent_key": key, "run_id": run_id,
                    })  # fmt: skip
                    sent += 1
            return sent

        return await for_each_tenant(per_tenant)

    return dict(run(job) or {})


__all__ = [
    "notify_regressions",
    "on_alternative_requested",
    "on_proposal_decided",
    "on_run_started",
    "schedule_live",
]

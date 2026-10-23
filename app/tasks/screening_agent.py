# ruff: file-ignore[line-too-long]
"""Screening Agent tasks (Epic 57). Every task starts with ``tenant_id`` (beat fan-out excepted). Queue ``maintenance``.

* ``screening_agent.channel_inbound`` / ``screening_agent.email_reply`` -- candidate replies on WhatsApp / email.
* ``screening_agent.consent_withdrawn`` -- ``ai_agent_communication`` / ``ai_processing`` withdrawn: stop at once.
* ``screening_agent.workflow_run`` -- a workflow "Start agent run" for ``screening``: the conversation is created and a
  conversation run with the proper kickoff supersedes the workflow run (a 409 becomes a recruiter Task).
* ``screening_agent.run_failed`` -- a failed screening run hands the conversation to the recruiter (``model_failure``).
* ``screening_agent.run_needed`` -- a reply arrived while a run was mid-step: retry the hand-over shortly.
* ``screening_agent.sweep`` (beat, every 15 minutes) -- reminders and expiry.
"""

import json
import uuid
from typing import Any

from loguru import logger

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import for_each_tenant, run

QUEUE = "maintenance"
RETRY_SECONDS = 15
MAX_RETRIES = 8


def _ctx(tid: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tid, actor_kind="system")


@celery_app.task(name="screening_agent.channel_inbound", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def channel_inbound(tenant_id: str, message_id: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from app.services.screening_agent import (  # ruff: ignore[unused-import]
            conversations,  # noqa: F401
            inbound,
            reminders,  # noqa: F401
        )

        async with tenant_session(tid) as db:
            return await inbound.on_channel_inbound(_ctx(tid), db, uuid.UUID(str(message_id)))

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="screening_agent.email_reply", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def email_reply(
    tenant_id: str, inbound_email_id: str, email_message_id: str, candidate_id: str | None
) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from app.services.screening_agent import (  # ruff: ignore[unused-import]
            conversations,  # noqa: F401
            inbound,
            reminders,  # noqa: F401
        )

        async with tenant_session(tid) as db:
            return await inbound.on_email_reply(
                _ctx(tid), db, uuid.UUID(str(inbound_email_id)), uuid.UUID(str(email_message_id)),
                uuid.UUID(str(candidate_id)) if candidate_id else None,
            )  # fmt: skip

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="screening_agent.consent_withdrawn", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def consent_withdrawn(tenant_id: str, candidate_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> int:
        from sqlalchemy import select

        from app.models.screening_agent import OPEN_STATUSES, ScreeningConversation
        from app.services.screening_agent import (  # ruff: ignore[unused-import]
            conversations,
            inbound,  # noqa: F401
            reminders,  # noqa: F401
        )

        async with tenant_session(tid) as db:
            rows = list(
                (
                    await db.execute(
                        select(ScreeningConversation)
                        .where(
                            ScreeningConversation.candidate_id == uuid.UUID(str(candidate_id)),
                            ScreeningConversation.status.in_(OPEN_STATUSES),
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            for conv in rows:
                await conversations.withdraw(_ctx(tid), db, conv)
            return len(rows)

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="screening_agent.workflow_run", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def workflow_run(tenant_id: str, run_id: str) -> str:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str:
        from app.core.errors import ConflictError
        from app.models.agent_runtime import AgentRun
        from app.services import agent_runtime_service as runtime
        from app.services.screening_agent import (  # ruff: ignore[unused-import]
            conversations,
            inbound,  # noqa: F401
            reminders,  # noqa: F401
        )

        async with tenant_session(tid) as db:
            row = await db.get(AgentRun, uuid.UUID(str(run_id)))
            if row is None or row.subject_type != "application" or row.subject_id is None:
                return "skipped"
            if row.status in ("queued", "running", "waiting"):
                await runtime.cancel_run(
                    _ctx(tid), db, row.id, reason="superseded by the screening conversation run"
                )
            try:
                async with db.begin_nested():
                    conv = await conversations.start(
                        _ctx(tid), db, row.subject_id, trigger_kind="workflow"
                    )
            except ConflictError as exc:
                from app.models.pipeline import Application
                from app.schemas.tasks import TaskCreate
                from app.services import tasks_service

                app = await db.get(Application, row.subject_id)
                if app is not None:
                    data = TaskCreate.model_construct(
                        title="AI screening could not start", notes=f"The workflow rule could not start AI screening: {exc.message}",
                        owner_id=app.owner_id, entity_type="application", entity_id=app.id, due_at=None, due_all_day=False,
                        priority="normal", reminder_at=None,
                    )  # fmt: skip
                    try:
                        async with db.begin_nested():
                            await tasks_service.create_system_task(_ctx(tid), db, data, source="agent")
                    except Exception:
                        logger.warning("Screening workflow task not created for application {}", app.id)
                return exc.code
            return str(conv.id)

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="screening_agent.run_failed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_failed(tenant_id: str, run_id: str, reason_code: str) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from sqlalchemy import select

        from app.models.screening_agent import ScreeningConversation
        from app.services.screening_agent import (  # ruff: ignore[unused-import]
            conversations,
            inbound,  # noqa: F401
            reminders,  # noqa: F401
        )

        async with tenant_session(tid) as db:
            conv = (
                await db.execute(
                    select(ScreeningConversation)
                    .where(ScreeningConversation.current_run_id == uuid.UUID(str(run_id)))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if conv is None or conv.status not in ("consented", "in_progress", "invited"):
                return None
            if reason_code in ("consent_missing",):
                await conversations.withdraw(_ctx(tid), db, conv)
                return "withdrawn"
            await conversations.handoff(
                _ctx(tid),
                db,
                conv,
                "model_failure",
                f"The AI assistant stopped ({reason_code}).",
                cancel=False,
            )
            return "handed_off"

    return run(job, tenant_id=tid)  # type: ignore[no-any-return]


@celery_app.task(name="screening_agent.run_needed", acks_late=True, bind=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def run_needed(
    self: Any,  # ruff: ignore[unused-function-argument]
    tenant_id: str,
    conversation_id: str,
    payload: str,
    attempt: int = 0,
) -> str | None:
    tid = uuid.UUID(str(tenant_id))

    async def job() -> str | None:
        from app.services.screening_agent import (  # ruff: ignore[unused-import]
            conversations,
            inbound,  # noqa: F401
            reminders,  # noqa: F401
        )
        from app.services.screening_agent.common import conversation

        data = json.loads(payload or "{}")
        async with tenant_session(tid) as db:
            conv = await conversation(db, uuid.UUID(str(conversation_id)), for_update=True)
            if conv.status not in ("consented", "in_progress", "invited"):
                return None
            reply = None
            if data.get("has_reply"):
                from app.services.screening_agent import turns

                last = [t for t in await turns.list_turns(db, conv.id) if t.direction == "inbound"]
                reply = last[-1].body if last else None
            return await conversations.wake(
                _ctx(tid), db, conv, payload=data.get("payload"), reply=reply, event=data.get("event")
            )

    outcome = run(job, tenant_id=tid)
    if outcome == "queued_retry" and attempt < MAX_RETRIES and not celery_app.conf.task_always_eager:
        run_needed.apply_async(
            args=(tenant_id, conversation_id, payload, attempt + 1), queue=QUEUE, countdown=RETRY_SECONDS
        )
    return outcome  # type: ignore[no-any-return]


@celery_app.task(name="screening_agent.sweep", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def sweep() -> dict[str, Any]:
    async def job(tid: uuid.UUID) -> Any:
        from app.services.screening_agent import (  # ruff: ignore[unused-import]
            conversations,  # noqa: F401
            inbound,  # noqa: F401
            reminders,
        )

        async with tenant_session(tid) as db:
            out = await reminders.sweep(db)
            return {
                "expired": out.expired,
                "reminder_runs": out.reminder_runs,
                "reminder_tasks": out.reminder_tasks,
            }

    return run(lambda: for_each_tenant(job))  # type: ignore[no-any-return]


__all__ = [
    "channel_inbound",
    "consent_withdrawn",
    "email_reply",
    "run_failed",
    "run_needed",
    "sweep",
    "workflow_run",
]

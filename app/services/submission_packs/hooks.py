# ruff: file-ignore[line-too-long]
"""Registrations of the ``submission_packs`` domain (Epic 61), autodiscovered by ``core.hooks``.

* events (after commit; each enqueues a ``submission_packs.*`` / ``agents.*`` task):
  - P1 ``ApplicationStageChanged`` to ``shortlisted`` by a person -> the Submission Agent's ``shortlisted`` trigger
    (kind ``inbound``, dedup ``shortlisted:<application>``: the foundation event wiring has no filter, so the filter
    lives here, as the Demand Agent's ``sla_changed`` does); leaving ``shortlisted`` (not to ``submitted``) -> the open
    pack is discarded (``application_moved``);
  - P1 ``SubmissionCreated`` -> ``link_submission`` (lifecycle ``used`` + diff, ``SubmissionPackUsed``);
  - ``ConsentGranted`` / ``ConsentWithdrawn`` (``client_submission``) -> re-check the candidate's open packs (no model);
  - ``AgentRunFailed`` / ``AgentRunFinished(stopped)`` of the ``submission`` agent -> a checks-only pack;
  - ``SubmissionPackReady`` (``ready`` / ``blocked``) -> notification ``submission_pack_ready``.
* the notification type, audit labels, settings hub entry, workflow trigger ``submission_packs.pack_ready``, the
  activity entity ``submission_pack`` and the retention binding (class ``agent_logs``: write-up, checks and the diff are
  blanked after 12 months).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe

AGENT_KEY = "submission"
PURPOSE = "client_submission"


def _enqueue(task: Any, *args: Any) -> None:
    from app.tasks.runtime import enqueue

    enqueue(task, *args)


# ---------------------------------------------------------------- events
def _subscribe() -> None:
    from app.core.actors import HUMAN_KINDS
    from app.events.agents import AgentRunFailed, AgentRunFinished
    from app.events.pipeline import ApplicationStageChanged, SubmissionCreated
    from app.events.privacy import ConsentGranted, ConsentWithdrawn
    from app.events.submission_packs import SubmissionPackReady

    def on_stage(e: ApplicationStageChanged) -> None:
        from app.tasks import agents as agent_tasks
        from app.tasks import submission_packs as tasks

        if e.to_key == "shortlisted" and e.actor_kind in HUMAN_KINDS:
            _enqueue(
                agent_tasks.start_from_event, str(e.tenant_id), AGENT_KEY, "shortlisted", "application",
                str(e.application_id), f"shortlisted:{e.application_id}",
            )  # fmt: skip
        elif e.from_key == "shortlisted" and e.to_key not in ("shortlisted", "submitted"):
            _enqueue(
                tasks.discard_for_application, str(e.tenant_id), str(e.application_id), "application_moved"
            )

    def on_created(e: SubmissionCreated) -> None:
        from app.tasks import submission_packs as tasks

        _enqueue(tasks.link_submission, str(e.tenant_id), str(e.submission_id))

    def on_consent(e: Any) -> None:
        from app.tasks import submission_packs as tasks

        if getattr(e, "purpose", None) == PURPOSE:
            _enqueue(tasks.recheck_candidate, str(e.tenant_id), str(e.candidate_id))

    def on_failed(e: Any) -> None:
        from app.tasks import submission_packs as tasks

        if getattr(e, "agent_key", None) != AGENT_KEY or getattr(e, "subject_type", None) != "application":
            return
        if isinstance(e, AgentRunFinished) and e.status != "stopped":
            return
        if e.subject_id is not None:
            _enqueue(tasks.checks_only, str(e.tenant_id), str(e.subject_id), str(e.run_id))

    def on_ready(e: SubmissionPackReady) -> None:
        from app.tasks import notifications as notification_tasks

        if e.status not in ("ready", "blocked"):
            return
        _enqueue(notification_tasks.fan_out, str(e.tenant_id), "submission_pack_ready", {
            "event_id": f"submission_pack:{e.pack_id}:{e.status}:{e.blocker_count}", "pack_id": str(e.pack_id),
            "application_id": str(e.application_id), "client_id": str(e.client_id), "status": e.status,
            "blocker_count": e.blocker_count, "agent_run_id": str(e.agent_run_id) if e.agent_run_id else None,
        })  # fmt: skip

    for fn, name in (
        (on_stage, "stage_changed"),
        (on_created, "submission_created"),
        (on_consent, "consent_changed"),
        (on_failed, "agent_run_failed"),
        (on_ready, "pack_ready"),
    ):
        fn.__name__ = f"submission_packs_on_{name}"
    subscribe(ApplicationStageChanged, on_stage)
    subscribe(SubmissionCreated, on_created)
    subscribe(ConsentGranted, on_consent)
    subscribe(ConsentWithdrawn, on_consent)
    subscribe(AgentRunFailed, on_failed)
    subscribe(AgentRunFinished, on_failed)
    subscribe(SubmissionPackReady, on_ready)


# ---------------------------------------------------------------- notifications (FR-327)
def _register_notifications() -> None:
    from app.services import notifications_service as ns

    def _uuid(v: Any) -> uuid.UUID | None:
        try:
            return uuid.UUID(str(v)) if v else None
        except ValueError:
            return None

    async def recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        from app.models.agent_runtime import AgentRun
        from app.models.pipeline import Application

        out: set[uuid.UUID] = set()
        app_id = _uuid(payload.get("application_id"))
        app = await db.get(Application, app_id) if app_id else None
        if app is not None:
            out.add(app.owner_id)
        run_id = _uuid(payload.get("agent_run_id"))
        run = await db.get(AgentRun, run_id) if run_id else None
        if run is not None:
            out.add(run.owner_user_id)
        return out

    async def render(db: AsyncSession, payload: Any) -> Any:
        from app.models.pipeline import Application
        from app.services import entity_refs

        app_id = _uuid(payload.get("application_id"))
        app = await db.get(Application, app_id) if app_id else None
        candidate = await entity_refs.entity_label(db, "candidate", app.candidate_id) if app else None
        client = await entity_refs.entity_label(db, "client", _uuid(payload.get("client_id")))
        n = int(payload.get("blocker_count") or 0)
        title = (
            f"Submission pack blocked: {n} check(s) need you"
            if payload.get("status") == "blocked"
            else f"Submission pack ready for {candidate or 'the candidate'} → {client or 'the client'}"
        )
        return ns.Content(
            title=title,
            entity_type="application",
            entity_id=app_id,
            link_path=f"/pipeline/applications/{app_id}?panel=submission-pack" if app_id else None,
        )

    if "submission_pack_ready" not in ns.NOTIFICATION_TYPES:
        ns.register_notification_type(
            "submission_pack_ready",
            ns.NotificationType(
                "Submission pack ready", "AI agents", "in_app", False, recipients, render, internal_only=True
            ),
        )


# ---------------------------------------------------------------- settings / audit / workflow / activity / retention
def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "client_submission_rules",
        "Client submission rules",
        "Resume format, contact masking, write-up length and duplicate window per client.",
        "/api/v1/clients/{client_id}/submission-rules",
        "pipeline:configure",
        owner_epic="Epic 61",
    )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("submission_pack.prepared", "A submission pack was prepared"),
        ("submission_pack.opened", "A submission pack was opened in the Submission dialog"),
        ("submission_pack.used", "A submission was sent from a submission pack"),
        ("submission_pack.discarded", "A submission pack was discarded"),
        ("submission_rules.updated", "A client's submission rules changed"),
    ):
        audit.register_audit_action(key, text)


def _register_workflow_trigger() -> None:
    try:
        from app.services.workflow.registry import TRIGGERS, FieldDef, Trigger, register_trigger
    except ImportError:  # pragma: no cover - workflow absent
        return
    key = "submission_packs.pack_ready"
    if key in TRIGGERS:
        return
    register_trigger(
        key,
        Trigger(
            key, "Submission pack ready or blocked", "app.events.submission_packs", "SubmissionPackReady", "application",
            "application_id",
            {
                "status": ("status", FieldDef("enum", "Pack status", ("draft", "ready", "blocked"))),
                "blocker_count": ("blocker_count", FieldDef("int", "Blocking checks")),
                "client_id": ("client_id", FieldDef("uuid", "Client")),
            },
        ),
    )  # fmt: skip


def _register_activity() -> None:
    from app.services import activity_service

    async def access(ctx: Any, db: AsyncSession, pack_id: uuid.UUID, write: bool) -> bool:
        from app.core.errors import DomainError
        from app.models.submission_packs import SubmissionPack
        from app.services.submission_packs.common import PACK_READ, SUBMIT, application_for

        pack = await db.get(SubmissionPack, pack_id)
        if pack is None:
            return False
        try:
            await application_for(ctx, db, pack.application_id, SUBMIT if write else PACK_READ)
        except DomainError:
            return False
        return True

    activity_service.register_entity("submission_pack", access, plural="submission-packs")


def _register_retention() -> None:
    from sqlalchemy import null, select, update

    from app.models.submission_packs import SubmissionPack
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    async def _subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
        return (
            await db.execute(select(SubmissionPack.candidate_id).where(SubmissionPack.id == row_id))
        ).scalar_one_or_none()

    async def _anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(select(SubmissionPack.updated_at).where(SubmissionPack.id == row_id))
        ).scalar_one_or_none()

    async def _expire(
        db: AsyncSession,
        ids: Sequence[uuid.UUID],
        action: str = "anonymize",  # ruff: ignore[unused-function-argument]
    ) -> dict[str, int]:
        result = await db.execute(
            update(SubmissionPack)
            .where(SubmissionPack.id.in_(list(ids)))
            .values(summary_text=None, checks_detail=[], user_changes=null())
        )
        return {"redacted": int(getattr(result, "rowcount", 0) or 0)}

    if "submission_packs" not in retention_bindings():
        register_retention_binding(
            "submission_packs",
            "agent_logs",
            subject_resolver=_subject,
            anchor_resolver=_anchor,
            expire_fn=_expire,
        )


for _fn in (
    _subscribe,
    _register_notifications,
    _register_settings,
    _register_audit,
    _register_workflow_trigger,
    _register_activity,
    _register_retention,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("submission_packs hooks: {} failed", _fn.__name__)
        raise

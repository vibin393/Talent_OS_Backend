# ruff: noqa: E501
"""Registrations of the ``fairness`` domain (autodiscovered by ``core.hooks``, a dynamic import -- not a static edge
for the ``fairness-isolation`` contract), Epic 49.

* The default ``demographic_self_id`` consent notice (``privacy.consent.register_default_notice``).
* The AEDT score suppressor (``matching.ports.register_score_suppressor``, coordinator decision 15).
* ``ConsentWithdrawn(purpose='demographic_self_id')`` -> ``fairness.delete_self_id`` (withdrawal through the privacy
  page or a DSR deletes the answers).
* ``RunRequested`` -> ``fairness.run_report`` (queue ``analytics``).
* Notification types ``fairness.run_completed`` (requester), ``fairness.alternative_requested`` (Demand owner, besides
  the Task) and ``fairness.publication_due`` (Compliance, ~11 months after the audit date).
* Audit action descriptions and the settings hub entry.
"""

import uuid
from typing import Any

from loguru import logger

NOTICE_TITLE = "Voluntary self-identification"
NOTICE_BODY = (
    "You can tell us your gender, age band and whether you have a disability. Answering is **optional** -- "
    "'Prefer not to say' is always available and skipping has no effect on your application.\n\n"
    "Your answers are encrypted and kept separately from your profile. Recruiters and hiring managers never see them "
    "and they are never used in any decision about you, by people or by automated tools. They are used only in "
    "aggregate, anonymised statistics that check our hiring tools treat all groups fairly (for example the bias audits "
    "required by New York City Local Law 144). Small groups are never shown.\n\n"
    "We do not ask about caste, religion or race/ethnicity. You can view, change or delete your answers at any time in "
    "the Candidate Portal; deleting them withdraws this consent."
)
AUDIT_ACTIONS = (
    (
        "self_id.updated",
        "A candidate's voluntary self-identification was updated (values are never recorded)",
    ),
    ("self_id.deleted", "A candidate's voluntary self-identification was deleted"),
    ("fairness.run_requested", "An adverse-impact report was requested"),
    ("fairness.run_viewed", "An adverse-impact report was viewed"),
    ("fairness.pack_exported", "An auditor pack was generated"),
    ("fairness.pack_downloaded", "An auditor pack file was downloaded"),
    ("fairness.summary_published", "A bias-audit summary was published on the Career Portal"),
    ("fairness.category_set_changed", "Self-identification category labels were changed"),
    ("fairness.alternative_requested", "A candidate requested an alternative selection process"),
)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


def _register_notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice("demographic_self_id", NOTICE_TITLE, NOTICE_BODY)


def _register_suppressor() -> None:
    from app.services.fairness.score_suppression import suppressor
    from app.services.matching.ports import register_score_suppressor

    register_score_suppressor(suppressor, key="fairness.aedt")


def on_consent_withdrawn(event: Any) -> None:
    if getattr(event, "purpose", None) != "demographic_self_id":
        return
    tid, cid = _uuid(getattr(event, "tenant_id", None)), _uuid(getattr(event, "candidate_id", None))
    if tid is None or cid is None:
        return
    from app.tasks import fairness as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.delete_self_id, str(tid), str(cid))


def on_run_requested(event: Any) -> None:
    from app.tasks import fairness as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.run_report, str(event.tenant_id), str(event.run_id))


def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.privacy import ConsentWithdrawn
    from app.services.fairness.events import RunRequested

    subscribe(ConsentWithdrawn, on_consent_withdrawn)
    subscribe(RunRequested, on_run_requested)


# ---------------------------------------------------------------- notifications
async def _requester(db: Any, payload: Any) -> set[Any]:  # noqa: ARG001
    uid = _uuid(payload.get("user_id"))
    return {uid} if uid else set()


async def _demand_owner(db: Any, payload: Any) -> set[Any]:
    from sqlalchemy import select

    from app.models.demands import Demand

    did = _uuid(payload.get("demand_id"))
    if did is None:
        return set()
    owner = (await db.execute(select(Demand.owner_id).where(Demand.id == did))).scalar_one_or_none()
    return {owner} if owner else set()


async def _compliance(db: Any, payload: Any) -> set[Any]:  # noqa: ARG001
    from app.services import entity_refs

    return set(await entity_refs.role_user_ids(db, "compliance")) | set(
        await entity_refs.role_user_ids(db, "tenant_admin")
    )


async def _render_run(db: Any, payload: Any) -> Any:  # noqa: ARG001
    from app.services.notifications_service import Content

    rid = _uuid(payload.get("run_id"))
    return Content(
        title="Your adverse-impact report is ready",
        body="Aggregate results only.",
        link_path=f"/fairness/runs/{rid}" if rid else "/fairness",
    )


async def _render_alt(db: Any, payload: Any) -> Any:  # noqa: ARG001
    from app.services.notifications_service import Content

    return Content(
        title="Alternative selection process requested",
        body="A candidate asked for an alternative to the automated tools. A task was created.",
        link_path="/tasks",
    )


async def _render_due(db: Any, payload: Any) -> Any:  # noqa: ARG001
    from app.services.notifications_service import Content

    return Content(
        title="Bias audit due",
        body="A published bias-audit summary is about to be a year old. Run a new audit.",
        link_path="/fairness",
    )


def _register_notifications() -> None:
    from app.services import notifications_service as ns
    from app.services.fairness.events import AlternativeRequested

    defs = (
        ("fairness.run_completed", "Adverse-impact report ready", _requester, _render_run),
        ("fairness.alternative_requested", "Alternative process requested", _demand_owner, _render_alt),
        ("fairness.publication_due", "Bias audit due", _compliance, _render_due),
    )
    for key, label, resolver, render in defs:
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(
                key,
                ns.NotificationType(
                    label, "Compliance", "in_app", False, resolver, render, internal_only=True
                ),
            )
    try:
        ns.register_event_source(AlternativeRequested, "fairness.alternative_requested")
    except Exception:  # pragma: no cover - already registered
        logger.debug("fairness: alternative_requested source already registered")


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "compliance",
        "fairness",
        "Bias audit & self-identification",
        "Voluntary self-ID (gender, age band, disability), small-cell threshold, NYC LL144 notice texts.",
        "/api/v1/fairness/settings",
        "fairness:configure",
        owner_epic="Epic 49",
    )


def _register_audit() -> None:
    from app.core import audit

    for key, desc in AUDIT_ACTIONS:
        audit.register_audit_action(key, desc)


for _fn in (
    _register_notice,
    _register_suppressor,
    _subscribe,
    _register_notifications,
    _register_settings,
    _register_audit,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("fairness: {} failed", _fn.__name__)

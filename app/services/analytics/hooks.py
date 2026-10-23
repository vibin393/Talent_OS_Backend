# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Registrations of the ``analytics`` domain (autodiscovered by ``core.hooks``), Epic 48.

* Workflow: the ``prediction`` snapshot loader and the notify / create-task restriction of
  ``analytics.prediction_band_changed`` (``workflow_policy``).
* Notification type ``prediction_band_changed`` (foundation 0.7 key): **opt-in** (``default_channel="off"``,
  ``mandatory_in_app=False``, internal only). Recipients: the Demand owner and assignees, the Offer owner or the
  Placement's account manager, filtered by the read permission of the kind (attrition -> only holders of
  ``prediction:read_attrition``).
* Event-driven scoring: Demand / Offer / Placement / extension / offboarding events mark the subject dirty; a
  Redis key debounces 5 minutes per subject, then ``analytics.score_subject`` runs on queue ``analytics``.
* The settings hub entry and audit action descriptions.

``analytics`` is a top-layer reader: no domain imports it (it subscribes to their events).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

AUDIT_ACTIONS = (
    ("analytics.model_trained", "A predictive model was trained"),
    ("analytics.settings_changed", "Predictive analytics settings were changed"),
)
NOTIFICATION_TYPE = "prediction_band_changed"
DEBOUNCE_SECONDS = 300


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- debounced scoring
_redis_client = None


def _redis() -> Any:  # redis client typing is loose
    global _redis_client  # process-wide lazy client
    if _redis_client is None:
        import redis

        from app.core.settings import get_settings

        s = get_settings()
        _redis_client = redis.Redis(
            host=s.redis_host,
            port=s.redis_port,
            password=s.redis_password or None,
            db=2,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=2,
        )
    return _redis_client


def debounce_acquire(tenant_id: uuid.UUID, subject_kind: str, subject_id: uuid.UUID) -> bool | None:
    """True = first event in the window (schedule a score), False = already scheduled, None = Redis down."""
    try:
        return bool(
            _redis().set(
                f"analytics:score:{tenant_id}:{subject_kind}:{subject_id}", "1", nx=True, ex=DEBOUNCE_SECONDS
            )
        )
    except Exception as exc:
        logger.warning("analytics debounce unavailable ({}); scoring directly", type(exc).__name__)
        return None


def mark_dirty(tenant_id: Any, subject_kind: str, subject_id: Any) -> None:
    tid, sid = _uuid(tenant_id), _uuid(subject_id)
    if tid is None or sid is None:
        return
    acquired = debounce_acquire(tid, subject_kind, sid)
    if acquired is False:
        return
    from app.tasks import analytics as tasks

    countdown = DEBOUNCE_SECONDS if acquired else 0
    try:
        if (
            countdown
            and not getattr(getattr(tasks.score_subject, "app", None), "conf", None).task_always_eager
        ):
            tasks.score_subject.apply_async(args=[str(tid), subject_kind, str(sid)], countdown=countdown)
            return
    except Exception:
        logger.exception("analytics: could not schedule score_subject")
        return
    from app.tasks.runtime import enqueue

    enqueue(tasks.score_subject, str(tid), subject_kind, str(sid))


def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events import resolve_event_class

    specs: tuple[tuple[str, str, str, str], ...] = (
        ("app.events.demands", "DemandOpened", "demand", "demand_id"),
        ("app.events.demands", "DemandRequirementsChanged", "demand", "demand_id"),
        ("app.events.demands", "DemandStatusChanged", "demand", "demand_id"),
        ("app.events.offers", "OfferAccepted", "offer", "offer_id"),
        ("app.events.offers", "OfferStatusChanged", "offer", "offer_id"),
        ("app.events.placements", "PlacementCreated", "placement", "placement_id"),
        ("app.events.placements", "PlacementStatusChanged", "placement", "placement_id"),
        ("app.events.placements", "PlacementActivated", "placement", "placement_id"),
        ("app.events.placements", "ExtensionStatusChanged", "placement", "placement_id"),
        ("app.events.placements", "OffboardingCaseStatusChanged", "placement", "placement_id"),
    )
    for module, name, subject_kind, attr in specs:
        cls = resolve_event_class(module, name)
        if cls is None:
            logger.debug("analytics: event {}.{} not available", module, name)
            continue

        def handler(event: object, _kind: str = subject_kind, _attr: str = attr) -> None:
            mark_dirty(getattr(event, "tenant_id", None), _kind, getattr(event, _attr, None))

        handler.__name__ = f"analytics_score_{name}"
        subscribe(cls, handler)


# ---------------------------------------------------------------- notifications
async def _recipients(db: AsyncSession, payload: Any) -> set[Any]:
    from app.models.demands import DemandAssignment
    from app.services import entity_refs
    from app.services.analytics.governance import kind_permission
    from app.services.analytics.workflow_policy import owner_of_subject

    subject_kind = str(payload.get("subject_kind") or "")
    sid = _uuid(payload.get("subject_id"))
    if sid is None or subject_kind not in ("demand", "offer", "placement"):
        return set()
    owner, _demand_id = await owner_of_subject(db, subject_kind, sid)
    users: set[uuid.UUID] = {owner} if owner else set()
    if subject_kind == "demand":
        users |= {
            u
            for u in (
                await db.execute(
                    select(DemandAssignment.user_id).where(
                        DemandAssignment.demand_id == sid, DemandAssignment.removed_at.is_(None)
                    )
                )
            ).scalars()
            if u
        }
    permission = kind_permission(str(payload.get("model_kind") or ""))
    tenant_id = entity_refs.session_tenant(db)
    out: set[Any] = set()
    for user_id in users:
        principal = await entity_refs.principal_for_user(db, tenant_id, user_id)
        if principal is not None and principal.has(permission):
            out.add(user_id)
    return out


LINKS = {"demand": "/demands/{id}", "offer": "/offers?offer={id}", "placement": "/placements/{id}"}


async def _render(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
    from app.services.analytics.features.specs import KIND_LABELS
    from app.services.notifications_service import Content

    kind = str(payload.get("model_kind") or "")
    subject_kind = str(payload.get("subject_kind") or "")
    sid = _uuid(payload.get("subject_id"))
    band = str(payload.get("to_band") or "").title()
    return Content(
        title=f"{KIND_LABELS.get(kind, 'Prediction')} is now {band}"[:300],
        body="Supporting signal, not a decision. Open the record to see the contributions.",
        entity_type=subject_kind or None,
        entity_id=sid,
        link_path=LINKS.get(subject_kind, "/").format(id=sid) if sid else None,
    )


def _register_notifications() -> None:
    from app.events.analytics import PredictionBandChanged
    from app.services import notifications_service as ns

    if NOTIFICATION_TYPE in ns.NOTIFICATION_TYPES:
        return
    ns.register_notification_type(
        NOTIFICATION_TYPE,
        ns.NotificationType(
            "Prediction band changed", "Predictions", "off", False, _recipients, _render, internal_only=True
        ),
    )
    ns.register_event_source(PredictionBandChanged, NOTIFICATION_TYPE)


# ---------------------------------------------------------------- workflow / settings / audit
def _register_workflow() -> None:
    from app.services.analytics import workflow_policy

    workflow_policy.register()


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "analytics",
        "Predictive analytics",
        "Time-to-fill, offer-drop and attrition models: enable or disable, data thresholds and risk bands.",
        "/api/v1/settings/analytics",
        "model:manage",
        owner_epic="Epic 48",
    )


def _register_audit() -> None:
    from app.core import audit

    for key, desc in AUDIT_ACTIONS:
        audit.register_audit_action(key, desc)


def _register_reader() -> None:
    """Expose the read helpers to the dashboards through ``core.prediction_port`` (nobody imports analytics)."""
    from app.core.prediction_port import register_prediction_reader
    from app.services.analytics import governance

    class _Reader:
        def can_read_kind(self, principal: Any, kind: str) -> bool:
            return governance.can_read_kind(principal, kind)

        async def model_cards(self, ctx: Any, db: AsyncSession) -> list[Any]:
            return list(await governance.model_cards(ctx, db))

    register_prediction_reader(_Reader())


for _fn in (
    _subscribe,
    _register_notifications,
    _register_workflow,
    _register_settings,
    _register_audit,
    _register_reader,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("analytics: {} failed", _fn.__name__)

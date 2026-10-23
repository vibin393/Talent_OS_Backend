"""Registrations of the ``placements`` domain (autodiscovered by ``core.hooks``).

* Approver resolvers ``placement_account_manager`` (Epic 13) and ``placement_manager`` (Epic 19).
* Approval subjects ``start_gate_override``, ``placement_rate_exception`` (Epic 16 owns ``rate_exception``) and
  ``bank_detail_change`` (human-only deciders, AD-24).
* Start-gate kinds ``contract`` and ``po`` (resolvers over the commercials read API).
* Notification types ``placements.start_gate_unmet``, ``placements.dropout_risk``,
  ``placements.demand_reopen_suggested``, ``placements.bank_change_requested``, ``placements.bank_change_decided``,
  ``placements.joining_question``; workflow triggers ``placements.created|activated|dropped|joining_confirmed|
  dropout_risk``.
* Event subscriptions: ``OfferAccepted`` -> ``placements.create_from_offer`` (queue ``interactive``); upstream BGV /
  onboarding / compliance / commercials events -> ``placements.upstream`` (gate rows and caches, AD-25).
* Merge sources for Epic 22 documents; the ``worker_profile`` retention binding (inert until Epic 35 seeds it).
"""

import dataclasses
import uuid
from collections.abc import Mapping
from typing import Any

from loguru import logger
from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe
from app.services.approvals import ResolvedApprover, register_approval_subject, register_approver_resolver

_placements = table("placements", column("id"), column("account_manager_user_id"), column("manager_user_id"))


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _resolve_user(db: AsyncSession, facts: Mapping[str, Any], col: str, label: str) -> ResolvedApprover:
    raw = facts.get("placement_id")
    if not raw:
        raise LookupError("placement_id")
    user_id = (
        await db.execute(select(getattr(_placements.c, col)).where(_placements.c.id == uuid.UUID(str(raw))))
    ).scalar_one_or_none()
    if user_id is None:
        raise LookupError(label)
    return ResolvedApprover("user", user_id, None, None, None, label)


async def _account_manager(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    approver_ref: Mapping[str, Any],  # ruff: ignore[unused-function-argument]
    facts: Mapping[str, Any],
) -> ResolvedApprover:
    return await _resolve_user(db, facts, "account_manager_user_id", "account manager")


async def _manager(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    approver_ref: Mapping[str, Any],  # ruff: ignore[unused-function-argument]
    facts: Mapping[str, Any],
) -> ResolvedApprover:
    return await _resolve_user(db, facts, "manager_user_id", "placement manager")


register_approver_resolver("placement_account_manager", _account_manager)
register_approver_resolver("placement_manager", _manager)


# ---------------------------------------------------------------- approval subjects
def _register_subjects() -> None:
    from app.services.placements import gates, rate_segments, workers

    register_approval_subject(
        gates.OVERRIDE_SUBJECT,
        on_final_decided=gates.on_override_decided,
        describe=gates.describe_overrides,
        auto_approve_calls_back=True,
    )
    register_approval_subject(
        rate_segments.EXCEPTION_SUBJECT,
        on_final_decided=rate_segments.on_exception_decided,
        describe=rate_segments.describe_exceptions,
        auto_approve_calls_back=True,
    )
    register_approval_subject(
        workers.BANK_SUBJECT,
        on_final_decided=workers.on_bank_change_decided,
        describe=workers.describe_bank_changes,
    )


_register_subjects()


# ---------------------------------------------------------------- start-gate kinds
def _register_gate_kinds() -> None:
    from app.services.placements import gates
    from app.services.placements.registry import StartGateKindDef, register_start_gate_kind, start_gate_kinds

    for kind, definition in (
        ("contract", StartGateKindDef("Client contract", overridable=True, resolver=gates.contract_resolver)),
        ("po", StartGateKindDef("Purchase order", overridable=True, resolver=gates.po_resolver)),
    ):
        if kind not in start_gate_kinds():
            register_start_gate_kind(kind, definition)


_register_gate_kinds()


# ---------------------------------------------------------------- event subscriptions
def _on_offer_accepted(event: Any) -> None:
    from app.tasks import placements as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.create_from_offer, str(event.tenant_id), str(event.offer_id))


def _upstream_handler(name: str) -> Any:
    def handle(event: Any) -> None:
        from app.services.notifications_service import event_payload
        from app.tasks import placements as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.upstream, str(event.tenant_id), name, event_payload(event))

    handle.__name__ = f"placements_on_{name}"
    return handle


_UPSTREAM = {
    "app.events.bgv": ("BgvCaseOpened", "BgvCaseStatusChanged", "BgvCaseAdjudicated"),
    "app.events.onboarding": (
        "OnboardingCaseCreated",
        "OnboardingProgressChanged",
        "OnboardingCaseCompleted",
        "OnboardingCaseCancelled",
    ),
    "app.events.compliance": ("CredentialStatusChanged", "PlacementComplianceChanged"),
    "app.events.commercials": ("PoValidityChanged", "ContractStatusChanged"),
}


def _subscribe() -> None:
    import importlib

    from app.events.offers import OfferAccepted

    subscribe(OfferAccepted, _on_offer_accepted)
    for module_name, names in _UPSTREAM.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover
            continue
        for name in names:
            cls = getattr(module, name, None)
            if cls is not None:
                subscribe(cls, _upstream_handler(name))


_subscribe()


# ---------------------------------------------------------------- notifications
async def _recruiter(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    rid = _uuid(payload.get("recruiter_user_id"))
    return {rid} if rid else set()


async def _listed(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    return {u for u in (_uuid(x) for x in payload.get("recipient_ids") or []) if u}


async def _owner(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    oid = _uuid(payload.get("owner_id"))
    return {oid} if oid else set()


async def _worker(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


async def _human(db: AsyncSession, placement_id: uuid.UUID | None) -> str:
    from app.models.placements import Placement

    if placement_id is None:
        return "A placement"
    human = (
        await db.execute(select(Placement.human_id).where(Placement.id == placement_id))
    ).scalar_one_or_none()
    return f"Placement {human}" if human else "A placement"


def _content(title: str, placement_id: uuid.UUID | None, body: str | None = None) -> Any:
    from app.services.notifications_service import Content

    return Content(
        title=title[:300],
        body=body,
        entity_type="placement" if placement_id else None,
        entity_id=placement_id,
        link_path=f"/placements/{placement_id}" if placement_id else None,
    )


async def _render_gates(db: AsyncSession, payload: Any) -> Any:
    pid = _uuid(payload.get("placement_id"))
    return _content(
        f"{await _human(db, pid)} starts {payload.get('planned_start_date')} with {payload.get('unmet')} unmet start gate(s)",
        pid,
    )


async def _render_dropout(db: AsyncSession, payload: Any) -> Any:
    pid = _uuid(payload.get("placement_id"))
    rules = ", ".join(payload.get("rules") or [])
    return _content(f"{await _human(db, pid)}: dropout risk ({rules})", pid)


async def _render_reopen(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    did = _uuid(payload.get("demand_id"))
    pid = _uuid(payload.get("placement_id"))
    return Content(
        title=f"{await _human(db, pid)} was dropped -- the demand is Filled; reopen it if you still need the position"[
            :300
        ],
        entity_type="demand",
        entity_id=did,
        link_path=f"/demands/{did}?action=reopen" if did else None,
    )


async def _render_question(db: AsyncSession, payload: Any) -> Any:
    pid = _uuid(payload.get("placement_id"))
    return _content(f"{await _human(db, pid)}: the worker has a question about joining", pid)


async def _render_bank_requested(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
    from app.services.notifications_service import Content

    return Content(
        title=f"Bank details change received (account ending {payload.get('last4') or '****'}) -- pending HR approval",
        body="If you did not request this change, contact your recruiter immediately.",
        link_path="/portal/work/bank",
    )


async def _render_bank_decided(db: AsyncSession, payload: Any) -> Any:
    from app.models.placements import WorkerBankAccount
    from app.services.notifications_service import Content

    last4 = None
    bid = _uuid(payload.get("bank_account_id"))
    if bid is not None:
        last4 = (
            await db.execute(
                select(WorkerBankAccount.account_number_last4).where(WorkerBankAccount.id == bid)
            )
        ).scalar_one_or_none()
    return Content(
        title=f"Your bank details change (account ending {last4 or '****'}) was {payload.get('outcome')}",
        body="If you did not request this change, contact your recruiter immediately.",
        link_path="/portal/work/bank",
    )


def _register_notifications() -> None:
    from app.events.placements import BankDetailsChangeDecided
    from app.services.notifications_service import (
        NotificationType,
        register_event_source,
        register_notification_type,
    )
    from app.services.placements import events as ev

    types = {
        "placements.start_gate_unmet": (
            NotificationType("Start gates unmet", "Workforce", "both", False, _listed, _render_gates),
            ev.StartGatesUnmet,
        ),
        "placements.dropout_risk": (
            NotificationType("Dropout risk", "Workforce", "both", False, _recruiter, _render_dropout),
            ev.DropoutRiskRaised,
        ),
        "placements.demand_reopen_suggested": (
            NotificationType(
                "Dropped placement on a filled demand", "Workforce", "both", False, _owner, _render_reopen
            ),
            ev.DemandReopenSuggested,
        ),
        "placements.joining_question": (
            NotificationType("Joining question", "Workforce", "both", False, _recruiter, _render_question),
            ev.JoiningQuestionRaised,
        ),
        "placements.bank_change_requested": (
            NotificationType(
                "Bank details change requested",
                "Workforce",
                "email",
                True,
                _worker,
                _render_bank_requested,
                "email",
            ),
            ev.BankDetailsChangeRequested,
        ),
        "placements.bank_change_decided": (
            NotificationType(
                "Bank details change decided",
                "Workforce",
                "email",
                True,
                _worker,
                _render_bank_decided,
                "email",
            ),
            BankDetailsChangeDecided,
        ),
    }
    for key, (definition, event_cls) in types.items():
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)
            continue
        register_event_source(event_cls, key)


_register_notifications()


# ---------------------------------------------------------------- workflow triggers
def _register_triggers() -> None:
    from app.services.workflow.registry import Trigger, register_trigger

    core = "app.events.placements"
    local = "app.services.placements.events"
    for trig in (
        Trigger(
            "placements.created", "Placement created", core, "PlacementCreated", "placement", "placement_id"
        ),
        Trigger(
            "placements.activated",
            "Placement joined",
            core,
            "PlacementActivated",
            "placement",
            "placement_id",
        ),
        Trigger(
            "placements.dropped", "Placement dropped", core, "PlacementDropped", "placement", "placement_id"
        ),
        Trigger(
            "placements.joining_confirmed",
            "Joining confirmed",
            local,
            "JoiningConfirmed",
            "placement",
            "placement_id",
        ),
        Trigger(
            "placements.dropout_risk",
            "Dropout risk raised",
            local,
            "DropoutRiskRaised",
            "placement",
            "placement_id",
        ),
    ):
        try:
            register_trigger(trig.key, trig)
        except Exception:
            logger.debug("trigger {} already registered", trig.key)


_register_triggers()


# ---------------------------------------------------------------- Epic 22 merge sources, retention binding
def _register_documents() -> None:
    from app.services.placements import merge_source

    try:
        merge_source.register()
    except Exception:
        logger.exception("placement merge sources not registered")


_register_documents()


@dataclasses.dataclass(frozen=True)
class _Anchor:
    table: str


async def _subject(db: AsyncSession, row_id: uuid.UUID, *, table_name: str) -> uuid.UUID | None:
    from app.models.placements import WorkerBankAccount, WorkerProfile

    model = WorkerProfile if table_name == "worker_profiles" else WorkerBankAccount
    return (await db.execute(select(model.candidate_id).where(model.id == row_id))).scalar_one_or_none()


async def _anchor(db: AsyncSession, candidate_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    """``placement_end``: the latest end date of the Worker's placements."""
    from sqlalchemy import func

    from app.models.placements import Placement

    return (
        await db.execute(
            select(func.max(func.coalesce(Placement.end_date, Placement.planned_end_date))).where(
                Placement.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()


async def _expire(*_: Any, **__: Any) -> None:
    """Retention expiry is executed by the erasure handler (``placements_erasure``)."""
    return


def _register_retention() -> None:
    try:
        from app.services.privacy.retention_classes import register_retention_binding
    except ImportError:  # pragma: no cover
        return
    for table_name, pii in (("worker_profiles", ("pan",)), ("worker_bank_accounts", ("account_number",))):
        try:

            async def subject(db: AsyncSession, row_id: uuid.UUID, _t: str = table_name) -> uuid.UUID | None:
                return await _subject(db, row_id, table_name=_t)

            register_retention_binding(
                table_name,
                "worker_profile",
                subject_resolver=subject,
                anchor_resolver=_anchor,
                expire_fn=_expire,
                pii_columns=pii,
            )
        except Exception:
            logger.debug("retention binding {} already registered", table_name)


_register_retention()


# ---------------------------------------------------------------- Epic 24 (bench management)
def _register_bench() -> None:
    from app.services.placements import bench_hooks

    bench_hooks.register()


_register_bench()


# ---------------------------------------------------------------- Epic 31 (extension, conversion & offboarding)
def _register_exit() -> None:
    from app.services.placements import exit_hooks

    exit_hooks.register()


_register_exit()


# ---------------------------------------------------------------- Epic 36: semantic base source
def _register_semantic_source() -> None:
    from app.services.placements import semantic_source

    semantic_source.register()


_register_semantic_source()

# ruff: noqa: E501, ARG001, N812
"""Registrations of the ``reporting`` domain (autodiscovered, Epic 34).

* Global search groups ``vendor``, ``placement``, ``worker``, ``invoice`` (A5) over read-only projections
  (the reporting read-model exception), so ``search`` imports no Phase 2 domain.
* After-commit handlers of ``InvoiceIssued``, ``InvoiceCancelled``, ``CreditNoteIssued``, ``DebitNoteIssued``
  and ``PlacementStatusChanged`` enqueue a debounced summary rebuild of the affected (LE, month) (A2).
* Notification type ``reporting_reconciliation_failed`` (Finance Admins, in-app, AD-37).
"""

import uuid
from collections.abc import Mapping
from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal, scope_condition
from app.models.candidates import Candidate
from app.models.clients import Client
from app.schemas.search import QuickHit
from app.services.reporting import p2tables as T
from app.services.reporting.common import available
from app.services.reporting.filters import perm


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    assert isinstance(principal, Principal)
    return principal


async def _has(db: AsyncSession, *tables: str) -> bool:
    return set(tables) <= await available(db)


# ================================================================ search groups (A5)
async def search_vendors(
    ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int
) -> list[QuickHit]:
    if not await _has(db, "vendors"):
        return []
    v = T.vendors.c
    term = q.strip()
    exact = func.upper(v.human_id) == term.upper()
    stmt = (
        select(v.id, v.human_id, v.legal_name, exact.label("exact"))
        .where(or_(exact, v.legal_name.ilike(f"{_escape(term)}%"), v.legal_name.op("%")(term)))
        .order_by(exact.desc(), func.similarity(v.legal_name, term).desc())
        .limit(limit)
    )
    return [
        QuickHit(id=r.id, type="vendor", title=r.legal_name, subtitle=r.human_id, human_id=r.human_id,
                 url=f"/vendors/{r.id}", exact=bool(r.exact))
        for r in (await db.execute(stmt)).all()
    ]  # fmt: skip


def _placement_scope(principal: Principal) -> Any:
    p = T.placements.c
    a = scope_condition(principal, "placement:read", p.recruiter_user_id)
    if a is None:
        return None
    b = scope_condition(principal, "placement:read", p.account_manager_user_id)
    return or_(a, b) if b is not None else a


async def search_placements(
    ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int
) -> list[QuickHit]:
    if not await _has(db, "placements"):
        return []
    p = T.placements.c
    term = q.strip()
    exact = func.upper(p.human_id) == term.upper()
    worker = select(Candidate.full_name).where(Candidate.id == p.candidate_id).scalar_subquery()
    client = select(Client.name).where(Client.id == p.client_id).scalar_subquery()
    cond = (
        exact
        if kind == "human_id"
        else or_(exact, worker.ilike(f"%{_escape(term)}%"), client.ilike(f"{_escape(term)}%"))
    )
    stmt = select(
        p.id, p.human_id, worker.label("worker"), client.label("client"), exact.label("exact")
    ).where(cond)
    scope = _placement_scope(_principal(ctx))
    if scope is not None:
        stmt = stmt.where(scope)
    rows = (await db.execute(stmt.order_by(exact.desc(), p.human_id).limit(limit))).all()
    return [
        QuickHit(id=r.id, type="placement", title=f"{r.human_id} · {r.worker or ''}".strip(" ·"),
                 subtitle=r.client, human_id=r.human_id, url=f"/placements/{r.id}", exact=bool(r.exact))
        for r in rows
    ]  # fmt: skip


async def search_workers(
    ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int
) -> list[QuickHit]:
    """Candidates with a non-terminal Placement, by name / email / phone (the P1 classifier)."""
    if not await _has(db, "placements"):
        return []
    from app.models.candidates import CandidateEmail, CandidatePhone
    from app.services.candidates.common import normalize_email, normalize_phone

    p = T.placements.c
    term = q.strip()
    live = select(p.candidate_id).where(p.status.not_in(("ended", "dropped")))
    scope = _placement_scope(_principal(ctx))
    if scope is not None:
        live = live.where(scope)
    stmt = select(Candidate.id, Candidate.full_name, Candidate.human_id).where(Candidate.id.in_(live))
    if kind == "email":
        email = normalize_email(term) or term.lower()
        stmt = stmt.where(
            Candidate.id.in_(select(CandidateEmail.candidate_id).where(CandidateEmail.email == email))
        )
    elif kind == "phone":
        phone = normalize_phone(term)
        if phone is None:
            return []
        stmt = stmt.where(
            Candidate.id.in_(select(CandidatePhone.candidate_id).where(CandidatePhone.phone_e164 == phone))
        )
    else:
        stmt = stmt.where(Candidate.full_name.ilike(f"%{_escape(term)}%"))
    rows = (await db.execute(stmt.order_by(Candidate.full_name).limit(limit))).all()
    return [
        QuickHit(
            id=r.id,
            type="worker",
            title=r.full_name,
            subtitle=r.human_id,
            human_id=r.human_id,
            url=f"/workers/{r.id}",
        )
        for r in rows
    ]


async def search_invoices(
    ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int
) -> list[QuickHit]:
    """Exact document number only (any series format, case-insensitive): Invoices, then Credit / Debit Notes."""
    if not await _has(db, "invoices"):
        return []
    term = q.strip().upper()
    principal = _principal(ctx)
    owner = scope_condition(principal, perm("invoice:read"), Client.owner_id)
    hits: list[QuickHit] = []
    for tbl, label, path in (
        (T.invoices, "Invoice", "/billing/invoices"),
        (T.credit_notes, "Credit note", "/billing/credit-notes"),
        (T.debit_notes, "Debit note", "/billing/debit-notes"),
    ):
        if tbl.name not in await available(db):
            continue
        c = tbl.c
        stmt = select(c.id, c.number, c.client_id).where(
            func.upper(c.number) == term, c.status.in_(("issued", "cancelled"))
        )
        if owner is not None:
            stmt = stmt.where(c.client_id.in_(select(Client.id).where(owner)))
        for r in (await db.execute(stmt.limit(limit))).all():
            name = (
                await db.execute(select(Client.name).where(Client.id == r.client_id))
            ).scalar_one_or_none()
            hits.append(
                QuickHit(id=r.id, type="invoice", title=f"{label} {r.number}", subtitle=name, human_id=r.number,
                         url=f"{path}/{r.id}", exact=tbl.name == "invoices")
            )  # fmt: skip
    return hits[:limit]


def _register_search() -> None:
    from app.services.search import global_search as gs

    if not hasattr(gs, "register_global_group"):
        return
    gs.register_global_group("vendor", "Vendors", "vendor:read", search_vendors)
    gs.register_global_group("placement", "Placements", "placement:read", search_placements)
    gs.register_global_group("worker", "Workers", "placement:read", search_workers)
    gs.register_global_group("invoice", "Invoices", perm("invoice:read"), search_invoices)
    # Epic 52 (A5, FR-262): Phase 3 groups delegate to the owners' quick_search functions.
    try:
        from app.services.reporting import search_p3

        search_p3.register()
    except Exception as exc:  # pragma: no cover - an owner import problem must not break startup
        logger.warning("Phase 3 search groups not registered ({})", exc)


# ================================================================ summary rebuild handlers (A2)
def enqueue_rebuild(tenant_id: uuid.UUID, legal_entity_id: uuid.UUID | None, day: date | None) -> None:
    if legal_entity_id is None or day is None:
        return
    try:
        from app.tasks import reporting as reporting_tasks
        from app.tasks.runtime import enqueue

        enqueue(
            reporting_tasks.rebuild_summaries_debounced,
            str(tenant_id),
            str(legal_entity_id),
            day.replace(day=1).isoformat(),
        )
    except Exception as exc:  # a rebuild trigger never breaks the business transaction's after-commit path
        logger.warning("Could not enqueue a summary rebuild ({})", type(exc).__name__)


def _on_invoice_issued(event: Any) -> None:
    enqueue_rebuild(event.tenant_id, event.legal_entity_id, event.doc_date)


def _on_document(kind: str, id_attr: str) -> Any:
    def handler(event: Any) -> None:
        from app.tasks import reporting as reporting_tasks
        from app.tasks.runtime import enqueue

        try:
            enqueue(
                reporting_tasks.rebuild_for_document, str(event.tenant_id), kind, str(getattr(event, id_attr))
            )
        except Exception as exc:
            logger.warning("Could not enqueue a summary rebuild ({})", type(exc).__name__)

    return handler


def _on_placement(event: Any) -> None:
    from app.tasks import reporting as reporting_tasks
    from app.tasks.runtime import enqueue

    try:
        enqueue(
            reporting_tasks.rebuild_for_document, str(event.tenant_id), "placement", str(event.placement_id)
        )
    except Exception as exc:
        logger.warning("Could not enqueue a summary rebuild ({})", type(exc).__name__)


def _register_handlers() -> None:
    from app.core.events import subscribe

    try:
        from app.events.billing import CreditNoteIssued, DebitNoteIssued, InvoiceCancelled, InvoiceIssued

        subscribe(InvoiceIssued, _on_invoice_issued)
        subscribe(InvoiceCancelled, _on_document("invoice", "invoice_id"))
        subscribe(CreditNoteIssued, _on_document("credit_note", "credit_note_id"))
        subscribe(DebitNoteIssued, _on_document("debit_note", "debit_note_id"))
    except ImportError:  # pragma: no cover - billing always present in Phase 2
        pass
    try:
        from app.events.placements import PlacementStatusChanged

        subscribe(PlacementStatusChanged, _on_placement)
    except ImportError:  # pragma: no cover
        pass


# ================================================================ notification type (AD-37)
async def _finance_admins(db: AsyncSession, payload: Mapping[str, Any]) -> set[uuid.UUID]:
    from app.services.reporting.common import role_user_ids

    return set(await role_user_ids(db, "finance_admin"))


async def _render(db: AsyncSession, payload: Mapping[str, Any]) -> Any:
    from app.services import notifications_service as notifications

    return notifications.Content(title="Reporting figures need attention", body=payload.get("body"))


def _register_notifications() -> None:
    from app.services import notifications_service as notifications

    key = "reporting_reconciliation_failed"
    if key not in notifications.NOTIFICATION_TYPES:
        notifications.register_notification_type(
            key,
            notifications.NotificationType(
                "Reporting reconciliation", "Reports", "in_app", True, _finance_admins, _render
            ),
        )


_registered = False


def register() -> None:
    global _registered  # process-wide wiring
    if _registered:
        return
    _register_search()
    _register_handlers()
    try:
        _register_notifications()
    except Exception as exc:  # pragma: no cover - signature drift must not break startup
        logger.warning("reporting notification type not registered ({})", exc)
    _registered = True


register()


# Epic 47 (additive): the custom report builder's sources, notification types, settings entry and handlers.
def _register_builder() -> None:
    try:
        from app.services.reporting.builder import hooks as builder_hooks

        builder_hooks.register()
    except Exception as exc:  # pragma: no cover - a builder registration problem must not break reporting
        logger.warning("report builder hooks not registered ({})", exc)


_register_builder()

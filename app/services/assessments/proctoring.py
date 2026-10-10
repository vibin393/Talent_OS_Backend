# ruff: noqa: E501
"""Proctoring-lite (Epic 40 A8, FR-206): append-only signals, annotations, display.

* Signals (``tab_switch``, ``focus_loss``, ``copy``, ``paste``, ``fullscreen_exit`` from the client;
  ``concurrent_session`` and ``ip_change`` recorded server-side) are rate-limited per attempt
  (``max_signals_per_minute`` / ``max_signals_per_attempt``); the excess is counted in ``attempts.dropped_signals``.
  ``detail`` is capped at 512 bytes. No webcam, microphone or screen capture exists.
* **Append-only:** this module exposes no update or delete path (``mutate_signal`` always raises); only the erasure
  handler deletes. Signals are advisory: no code path in lifecycle / scoring / review / results reads them.
"""

import json
import uuid
from datetime import timedelta
from typing import Any, NoReturn

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.assessments import Attempt, ProctoringAnnotation, ProctoringSignal
from app.schemas.assessments import AnnotationIn, AnnotationOut, SignalOut, SignalsOut
from app.schemas.assessments_delivery import SignalsAcceptedOut, SignalsIn
from app.services.assessments import settings as settings_mod
from app.services.assessments.common import not_found, now, user_id

MAX_DETAIL_BYTES = 512


class AppendOnlyViolation(RuntimeError):  # noqa: N818
    pass


def mutate_signal(*_: Any, **__: Any) -> NoReturn:
    """Signals are append-only: there is no update or delete path (erasure aside)."""
    msg = "proctoring signals are append-only"
    raise AppendOnlyViolation(msg)


def _detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    if not detail:
        return None
    try:
        raw = json.dumps(detail, default=str)
    except (TypeError, ValueError):
        return {"truncated": True}
    return detail if len(raw.encode()) <= MAX_DETAIL_BYTES else {"truncated": True}


def server_signal(db: AsyncSession, att: Attempt, kind: str, detail: dict[str, Any] | None = None) -> None:
    """``concurrent_session`` / ``ip_change``: always recorded (not rate-limited)."""
    db.add(
        ProctoringSignal(
            id=uuid.uuid4(),
            tenant_id=att.tenant_id,
            attempt_id=att.id,
            kind=kind,
            occurred_at=now(),
            detail=_detail(detail),
        )
    )
    att.signal_count += 1


async def record(db: AsyncSession, att: Attempt, data: SignalsIn) -> SignalsAcceptedOut:
    s = await settings_mod.current(db)
    recent = (
        await db.execute(
            select(func.count()).where(
                ProctoringSignal.attempt_id == att.id,
                ProctoringSignal.occurred_at >= now() - timedelta(minutes=1),
            )
        )
    ).scalar_one()
    accepted = 0
    dropped = 0
    for sig in data.signals:
        if recent + accepted >= s.max_signals_per_minute or att.signal_count >= s.max_signals_per_attempt:
            dropped += 1
            continue
        db.add(
            ProctoringSignal(
                id=uuid.uuid4(), tenant_id=att.tenant_id, attempt_id=att.id, kind=sig.kind, occurred_at=now(),
                client_ts=sig.client_ts, detail=_detail(sig.detail),
            )
        )  # fmt: skip
        att.signal_count += 1
        accepted += 1
    att.dropped_signals += dropped
    await db.flush()
    return SignalsAcceptedOut(accepted=accepted, dropped=dropped)


async def _attempt(db: AsyncSession, attempt_id: uuid.UUID) -> Attempt:
    att = await db.get(Attempt, attempt_id)
    if att is None:
        raise not_found("Attempt")
    return att


async def list_signals(ctx: RequestContext, db: AsyncSession, attempt_id: uuid.UUID) -> SignalsOut:  # noqa: ARG001
    att = await _attempt(db, attempt_id)
    rows = list(
        (
            await db.execute(
                select(ProctoringSignal)
                .where(ProctoringSignal.attempt_id == att.id)
                .order_by(ProctoringSignal.occurred_at)
            )
        ).scalars()
    )
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.kind] = counts.get(r.kind, 0) + 1
    notes = (
        await db.execute(
            select(ProctoringAnnotation)
            .where(ProctoringAnnotation.attempt_id == att.id)
            .order_by(ProctoringAnnotation.created_at)
        )
    ).scalars()
    return SignalsOut(
        counts=counts,
        dropped=att.dropped_signals,
        timeline=[
            SignalOut(id=r.id, kind=r.kind, occurred_at=r.occurred_at, client_ts=r.client_ts, detail=r.detail)
            for r in rows
        ],
        annotations=[
            AnnotationOut(
                id=n.id, signal_id=n.signal_id, note=n.note, created_by=n.created_by, created_at=n.created_at
            )
            for n in notes
        ],
    )


async def annotate(
    ctx: RequestContext, db: AsyncSession, attempt_id: uuid.UUID, data: AnnotationIn
) -> AnnotationOut:
    att = await _attempt(db, attempt_id)
    if data.signal_id is not None:
        sig = await db.get(ProctoringSignal, data.signal_id)
        if sig is None or sig.attempt_id != att.id:
            raise not_found("Signal")
    row = ProctoringAnnotation(
        id=uuid.uuid4(),
        tenant_id=att.tenant_id,
        attempt_id=att.id,
        signal_id=data.signal_id,
        note=data.note,
        created_by=user_id(ctx),
        created_at=now(),
    )
    db.add(row)
    await db.flush()
    return AnnotationOut(
        id=row.id,
        signal_id=row.signal_id,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )


__all__ = ["AppendOnlyViolation", "annotate", "list_signals", "mutate_signal", "record", "server_signal"]

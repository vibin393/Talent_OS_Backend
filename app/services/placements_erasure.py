"""Placements-domain erasure handler ``placements.worker`` (57), Epic 19 A12 (AD-34, AD-35).

Claims ``placements``, ``placement_status_history``, ``placement_touchpoints``, ``start_gate_overrides``,
``worker_profiles`` and ``worker_bank_accounts``.

* Placement facts (ids, parties, dates, segments) are kept: they are not personal beyond ``candidate_id`` and are
  needed for financial traceability.
* Free text becomes ``[erased]`` / NULL: ``drop_note``, ``counter_offer_note``, history notes, touchpoint
  ``response_note`` and override reasons.
* Emergency contact and PAN name are cleared (their audit values are crypto-shredded via ``__pii_fields__``).
* PAN and bank numbers are nulled -- except rows in ``ctx.retained`` (class ``worker_profile``), which keep their
  values and are reported as retained.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.placements import (
    Placement,
    PlacementStatusHistory,
    PlacementTouchpoint,
    StartGateOverride,
    WorkerBankAccount,
    WorkerProfile,
)
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"
RETENTION_CLASS = "worker_profile"


def _retained(ctx: ErasureContext, table: str, row_id: uuid.UUID) -> bool:
    return row_id in ctx.retained.get(table, set())


class PlacementsHandler:
    name = "placements.worker"
    order = 57
    tables = (
        "placements",
        "placement_status_history",
        "placement_touchpoints",
        "start_gate_overrides",
        "worker_profiles",
        "worker_bank_accounts",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        counts = {"placements": 0, "profiles": 0, "bank_accounts": 0, "retained": 0}
        placements = list(
            (await db.execute(select(Placement).where(Placement.candidate_id.in_(ids)))).scalars()
        )
        pids = [p.id for p in placements] or [uuid.uuid4()]
        for p in placements:
            p.drop_note = ERASED if p.drop_note else None
            p.counter_offer_note = ERASED if p.counter_offer_note else None
            counts["placements"] += 1
        for h in (
            await db.execute(
                select(PlacementStatusHistory).where(PlacementStatusHistory.placement_id.in_(pids))
            )
        ).scalars():
            h.note = None
        for t in (
            await db.execute(select(PlacementTouchpoint).where(PlacementTouchpoint.placement_id.in_(pids)))
        ).scalars():
            t.response_note = None
        for o in (
            await db.execute(select(StartGateOverride).where(StartGateOverride.placement_id.in_(pids)))
        ).scalars():
            o.reason = ERASED
        for prof in (
            await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id.in_(ids)))
        ).scalars():
            prof.emergency_name = prof.emergency_relation = prof.emergency_phone = None
            if _retained(ctx, "worker_profiles", prof.id):
                counts["retained"] += 1
            else:
                prof.pan = None
                prof.pan_bidx = None
                prof.pan_name = None
            counts["profiles"] += 1
        for bank in (
            await db.execute(select(WorkerBankAccount).where(WorkerBankAccount.candidate_id.in_(ids)))
        ).scalars():
            if _retained(ctx, "worker_bank_accounts", bank.id):
                counts["retained"] += 1
                continue
            bank.account_number = None
            bank.account_number_bidx = None
            bank.holder_name = None
            counts["bank_accounts"] += 1
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        placements = list(
            (await db.execute(select(Placement).where(Placement.candidate_id == ctx.candidate_id))).scalars()
        )
        profile = (
            await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == ctx.candidate_id))
        ).scalar_one_or_none()
        banks = list(
            (
                await db.execute(
                    select(WorkerBankAccount).where(WorkerBankAccount.candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        if not placements and profile is None and not banks:
            return None
        return {
            "worker": None
            if profile is None
            else {
                "worker_id": profile.worker_human_id,
                "emergency_name": profile.emergency_name,
                "emergency_relation": profile.emergency_relation,
                "emergency_phone": profile.emergency_phone,
                "pan_last4": profile.pan_last4,
                "pan_status": profile.pan_status,
                "retention_class": RETENTION_CLASS,
            },
            "placements": [
                {
                    "reference": p.human_id,
                    "status": p.status,
                    "role": p.role_title,
                    "planned_start_date": p.planned_start_date,
                    "actual_start_date": p.actual_start_date,
                    "end_date": p.end_date or p.planned_end_date,
                }
                for p in placements
            ],
            "bank_accounts": [
                {
                    "account_last4": b.account_number_last4,
                    "ifsc": b.ifsc,
                    "status": b.status,
                    "created_at": b.created_at,
                }
                for b in banks
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        for prof in (
            await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id.in_(ids)))
        ).scalars():
            if prof.emergency_name or prof.emergency_phone or prof.emergency_relation:
                findings.append(f"emergency contact remains on {prof.worker_human_id}")
            if prof.pan is not None and not _retained(ctx, "worker_profiles", prof.id):
                findings.append(f"PAN remains on {prof.worker_human_id}")
        for bank in (
            await db.execute(select(WorkerBankAccount).where(WorkerBankAccount.candidate_id.in_(ids)))
        ).scalars():
            if bank.account_number is not None and not _retained(ctx, "worker_bank_accounts", bank.id):
                findings.append("bank account number remains")
        for p in (await db.execute(select(Placement).where(Placement.candidate_id.in_(ids)))).scalars():
            if p.drop_note not in (None, ERASED) or p.counter_offer_note not in (None, ERASED):
                findings.append(f"free text remains on {p.human_id}")
        return findings


register_erasure_handler(PlacementsHandler())

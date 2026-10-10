"""Merge sources of the placements domain for Epic 22's document templates (``register_merge_source``).

Namespaces ``worker``, ``placement``, ``rate_segment``, ``client``, ``legal_entity`` -- all keyed by
``placement_id`` (``merge_refs["placement_id"]``). Only the catalogue attributes are returned (never Aadhaar, never
full PAN / bank numbers).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.placements import Placement, RateSegment, WorkerProfile

REF_KEY = "placement_id"


async def _placement(db: AsyncSession, placement_id: uuid.UUID) -> Placement:
    p = (await db.execute(select(Placement).where(Placement.id == placement_id))).scalar_one_or_none()
    if p is None:
        msg = "placement not found"
        raise LookupError(msg)
    return p


def _address(raw: Any) -> str:
    if isinstance(raw, Mapping):
        parts = [raw.get(k) for k in ("line1", "line2", "city", "state", "postal_code")]
        return ", ".join(str(x) for x in parts if x)
    return str(raw or "")


async def worker(db: AsyncSession, placement_id: uuid.UUID) -> Mapping[str, Any]:
    from app.services import entity_refs

    p = await _placement(db, placement_id)
    who = await entity_refs.addressee(db, "candidate", p.candidate_id)
    wrk = (
        await db.execute(
            select(WorkerProfile.worker_human_id).where(WorkerProfile.candidate_id == p.candidate_id)
        )
    ).scalar_one_or_none()
    return {
        "full_name": who.full_name if who else "",
        "email": who.address if who else "",
        "phone": "",
        "worker_id": wrk or "",
        "address": "",
    }


async def placement(db: AsyncSession, placement_id: uuid.UUID) -> Mapping[str, Any]:
    p = await _placement(db, placement_id)
    return {
        "id": p.human_id,
        "role": p.role_title or "",
        "start_date": p.actual_start_date or p.planned_start_date,
        "end_date": p.planned_end_date,
        "location": p.work_location_text or "",
        "worker_type": p.worker_type or "",
    }


async def rate_segment(db: AsyncSession, placement_id: uuid.UUID) -> Mapping[str, Any]:
    p = await _placement(db, placement_id)
    seg = (
        await db.execute(
            select(RateSegment)
            .where(RateSegment.placement_id == p.id, RateSegment.status == "active")
            .order_by(RateSegment.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if seg is None:
        return {
            "pay_rate": None,
            "bill_rate": None,
            "currency": p.currency,
            "rate_unit": "",
            "effective_from": None,
        }
    return {
        "pay_rate": seg.pay_rate,
        "bill_rate": seg.bill_rate,
        "currency": seg.currency,
        "rate_unit": seg.rate_unit,
        "effective_from": seg.effective_from,
    }


async def client(db: AsyncSession, placement_id: uuid.UUID) -> Mapping[str, Any]:
    from app.models.clients import Client

    p = await _placement(db, placement_id)
    name = (await db.execute(select(Client.name).where(Client.id == p.client_id))).scalar_one_or_none()
    return {"name": name or "", "address": ""}


async def legal_entity(db: AsyncSession, placement_id: uuid.UUID) -> Mapping[str, Any]:
    from app.models.legal_entities import GstRegistration, LegalEntity

    p = await _placement(db, placement_id)
    le = (await db.execute(select(LegalEntity).where(LegalEntity.id == p.legal_entity_id))).scalar_one()
    gstin = None
    if p.supplier_gst_registration_id is not None:
        gstin = (
            await db.execute(
                select(GstRegistration.gstin).where(GstRegistration.id == p.supplier_gst_registration_id)
            )
        ).scalar_one_or_none()
    return {
        "name": le.legal_name or le.name,
        "address": _address(le.address),
        "pan_last4": (le.pan or "")[-4:],
        "gstin": gstin or "",
    }


SOURCES = {
    "worker": worker,
    "placement": placement,
    "rate_segment": rate_segment,
    "client": client,
    "legal_entity": legal_entity,
}


def register() -> None:
    """Register with Epic 22's merge-source registry when present (guarded)."""
    try:
        from app.services.documents_service import register_merge_source
    except ImportError:  # pragma: no cover - Epic 22 not merged
        return
    for namespace, loader in SOURCES.items():
        register_merge_source(namespace, loader, ref_key=REF_KEY)

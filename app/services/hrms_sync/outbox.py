# ruff: file-ignore[line-too-long]
"""Internal outbox event types owned by Epic 71 (spec A4; visibility ``internal``: connector triggers only, never
webhooks or docs, FR-334): ``placement.joined``, ``placement.changed`` and ``payroll_export.finalized``."""

import dataclasses
import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.core.public_events import PublicEventSpec, catalogue, register_public_event
from app.events.payroll import PayrollExportFinalized
from app.events.placements import ExtensionConfirmed, PlacementActivated, PlacementConverted, RateSegmentAdded

__all__ = ["register"]


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlacementJoinedData(_Data):
    id: uuid.UUID
    candidate_id: uuid.UUID
    start_date: date


class PlacementChangedData(_Data):
    id: uuid.UUID
    change: Literal["rate_segment", "extension", "conversion"]
    effective_date: date


class PayrollExportFinalizedData(_Data):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    pay_period_id: uuid.UUID


def _spec(type_: str, source: type, subject_type: str, subject: Any, model: type[BaseModel], build: Any, description: str) -> PublicEventSpec:
    return PublicEventSpec(
        type=type_, version=1, source=source, subject_type=subject_type, subject_id=subject, data_model=model, build=build,
        scopes=(), visibility="internal", owner_epic="71", description=description,
    )  # fmt: skip


SPECS = (
    _spec("placement.joined", PlacementActivated, "placement", lambda e: e.placement_id, PlacementJoinedData,
          lambda e: PlacementJoinedData(id=e.placement_id, candidate_id=e.candidate_id, start_date=e.actual_start_date), "A Worker was marked Joined."),
    _spec("placement.changed", RateSegmentAdded, "placement", lambda e: e.placement_id, PlacementChangedData,
          lambda e: PlacementChangedData(id=e.placement_id, change="rate_segment", effective_date=e.effective_from), "A rate segment changed the placement."),
    _spec("placement.changed", ExtensionConfirmed, "placement", lambda e: e.placement_id, PlacementChangedData,
          lambda e: PlacementChangedData(id=e.placement_id, change="extension", effective_date=e.new_end_date), "A placement was extended."),
    _spec("placement.changed", PlacementConverted, "placement", lambda e: e.placement_id, PlacementChangedData,
          lambda e: PlacementChangedData(id=e.placement_id, change="conversion", effective_date=e.conversion_date), "A placement was converted."),
    _spec("payroll_export.finalized", PayrollExportFinalized, "payroll_export", lambda e: e.export_version_id, PayrollExportFinalizedData,
          lambda e: PayrollExportFinalizedData(id=e.export_version_id, legal_entity_id=e.legal_entity_id, pay_period_id=e.pay_period_id), "A payroll export was finalised."),
)  # fmt: skip


def register() -> None:
    """Idempotent: ``placement.changed`` has three sources, so versions 1..3 carry them (same type, one per source)."""
    known = catalogue(include_internal=True)
    versions: dict[str, int] = {}
    for spec in SPECS:
        versions[spec.type] = versions.get(spec.type, 0) + 1
        if spec.type in known and known[spec.type].version >= versions[spec.type]:
            continue
        register_public_event(dataclasses.replace(spec, version=versions[spec.type]))

"""Predictive analytics events (Epic 48). ``PredictionBandChanged`` backs the workflow trigger
``analytics.prediction_band_changed``.

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.
"""

import dataclasses
import uuid

__all__ = ["PredictionBandChanged"]


@dataclasses.dataclass(frozen=True, slots=True)
class PredictionBandChanged:
    tenant_id: uuid.UUID
    prediction_id: uuid.UUID
    model_kind: str
    subject_kind: str  # demand | offer | placement
    subject_id: uuid.UUID
    from_band: str | None
    to_band: str

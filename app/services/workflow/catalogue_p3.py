# ruff: noqa: E501
"""Phase 3 workflow catalogue (Epic 36 contract, spec 0.7): action keys and the ``analytics.prediction_band_changed``
trigger. Imported at the end of ``catalogue`` so the keys join ``ACTION_CATALOGUE`` on import;
``catalogue.register_catalogue`` registers ``P3_TRIGGER_CATALOGUE`` beside the Phase 2 triggers.

* **Actions** are contract keys only: each owning epic registers its ``ActionDef`` from its ``hooks.py``.
* **Trigger** ``analytics.prediction_band_changed`` subscribes (``subscribe_optional``) to
  ``app.events.analytics.PredictionBandChanged``; Epic 48 registers the ``prediction`` snapshot loader and
  restricts its actions to notify / task / escalate.
"""

from app.services.workflow.catalogue import ACTION_CATALOGUE, TriggerSpec
from app.services.workflow.conditions import FieldDef

__all__ = ["P3_ACTION_CATALOGUE", "P3_TRIGGER_CATALOGUE", "P3_TRIGGER_KEYS"]

# Contract-only action keys -> owner epic.
P3_ACTION_CATALOGUE: dict[str, str] = {
    "talent_pools.add_to_pool": "38",
    "campaigns.enrol_in_sequence": "39",
    "assessments.invite": "40",
}

P3_TRIGGER_CATALOGUE: tuple[TriggerSpec, ...] = (
    TriggerSpec(
        "analytics.prediction_band_changed", "Prediction band changed", "app.events.analytics", "PredictionBandChanged",
        "prediction", "prediction_id", "48",
        {
            "model_kind": ("model_kind", FieldDef("str", "Model")),
            "subject_kind": ("subject_kind", FieldDef("enum", "Subject", ("demand", "offer", "placement"))),
            "to_band": ("to_band", FieldDef("str", "New band")),
        },
    ),
)  # fmt: skip

P3_TRIGGER_KEYS: tuple[str, ...] = tuple(spec.key for spec in P3_TRIGGER_CATALOGUE)

ACTION_CATALOGUE.update(P3_ACTION_CATALOGUE)

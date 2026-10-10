# ruff: noqa: E501
"""Epic 36 (spec 0.7): the Phase 3 workflow catalogue is a binding contract.

The P3 action keys and the ``analytics.prediction_band_changed`` trigger are declared by
``services/workflow/catalogue_p3.py``; owners register the ``ActionDef`` / snapshot loader from their ``hooks.py``.
A key not registered yet must be listed in the pending dicts below with its owner epic, and a key that becomes
registered must be removed (shrink-only; the test enforces both directions, as ``test_p2_catalogue.py``).
"""

import dataclasses

from app.core.hooks import autodiscover
from app.events import resolve_event_class
from app.services.workflow import catalogue, catalogue_p3
from app.services.workflow.actions import ACTIONS
from app.services.workflow.registry import TRIGGERS

# trigger key -> owner epic (entity snapshot loader not registered yet)
PENDING_TRIGGER_LOADERS: dict[str, str] = {}  # Epic 48 registered the ``prediction`` loader
# action key -> owner epic (ActionDef not registered yet)
PENDING_ACTIONS: dict[str, str] = {}  # Epic 39 registered ``campaigns.enrol_in_sequence``


def _discover() -> None:
    autodiscover("app.services")


def test_p3_keys_are_the_contract() -> None:
    assert catalogue_p3.P3_ACTION_CATALOGUE == {
        "talent_pools.add_to_pool": "38",
        "campaigns.enrol_in_sequence": "39",
        "assessments.invite": "40",
    }
    assert catalogue_p3.P3_TRIGGER_KEYS == ("analytics.prediction_band_changed",)
    # P3 actions join the shared action catalogue (so owners' registrations pass the FR-181 / catalogue checks)
    assert set(catalogue_p3.P3_ACTION_CATALOGUE.items()) <= set(catalogue.ACTION_CATALOGUE.items())
    assert not set(catalogue_p3.P3_TRIGGER_KEYS) & {s.key for s in catalogue.TRIGGER_CATALOGUE}


def test_p3_trigger_registered_on_its_event() -> None:
    _discover()
    catalogue.register_catalogue()
    (spec,) = catalogue_p3.P3_TRIGGER_CATALOGUE
    assert spec.key in TRIGGERS
    assert catalogue.owner_of(spec.key) == "48"
    trigger = TRIGGERS[spec.key]
    assert (trigger.entity_type, trigger.entity_attr) == ("prediction", "prediction_id")
    cls = resolve_event_class(spec.module, spec.event_class)
    assert cls is not None
    names = {f.name for f in dataclasses.fields(cls)}
    assert spec.entity_attr in names
    assert set(spec.extras) == {"model_kind", "subject_kind", "to_band"}
    assert {attr for attr, _ in spec.extras.values()} <= names


def test_p3_trigger_loader_pending_list() -> None:
    _discover()
    loaders = catalogue.snapshot_loaders()
    for spec in catalogue_p3.P3_TRIGGER_CATALOGUE:
        has_loader = spec.entity_type in loaders
        if spec.key in PENDING_TRIGGER_LOADERS:
            assert PENDING_TRIGGER_LOADERS[spec.key] == spec.owner_epic
            assert not has_loader, (
                f"{spec.key} now has a snapshot loader: remove it from PENDING_TRIGGER_LOADERS"
            )
        else:
            assert has_loader, f"{spec.key} ({spec.entity_type}) has no snapshot loader and is not pending"
    assert set(PENDING_TRIGGER_LOADERS) <= set(catalogue_p3.P3_TRIGGER_KEYS)


def test_p3_action_pending_list() -> None:
    _discover()
    assert set(PENDING_ACTIONS) <= set(catalogue_p3.P3_ACTION_CATALOGUE)
    for key, owner in catalogue_p3.P3_ACTION_CATALOGUE.items():
        if key in PENDING_ACTIONS:
            assert PENDING_ACTIONS[key] == owner
            assert key not in ACTIONS, f"{key} is registered now: remove it from PENDING_ACTIONS"
        else:
            assert key in ACTIONS, f"{key} is neither registered nor pending"

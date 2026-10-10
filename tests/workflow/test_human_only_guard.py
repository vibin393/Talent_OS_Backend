# ruff: noqa: D400, D415, E501
"""FR-181 / AD-4 human-only guard for workflow v2 (Epic 32, story 19.4) -- three layers:

1. ``register_action`` refuses an action declaring a human-only transition;
2. actions run as actor ``workflow`` (P1 ``tests/workflow/test_human_only.py`` covers the run-time refusal);
3. no registered action performs a starred transition, graphs refuse human-only targets at save, and neither
   ``services/workflow`` nor ``services/sla`` references ``decide(`` or a starred service function.
"""

from pathlib import Path

import pytest

from app.core.hooks import autodiscover
from app.core.registry import RegistryError
from app.services.workflow import catalogue
from app.services.workflow.actions import ACTIONS, HUMAN_ONLY_STAGES, ActionDef, AddTagParams, register_action

STARRED = (
    "issue_invoice", "record_payment", "adjudicate", "finalize_export", "close_period", "write_off",
    "accept_vendor_submission",
)  # fmt: skip


async def _noop(ac: object) -> str:
    return "noop"


def _definition(*transitions: tuple[str, str]) -> ActionDef:
    def _any(params: object, entity_type: str, path: str) -> None:
        return None

    return ActionDef("Test", AddTagParams, _any, _noop, _noop, performs_transitions=tuple(transitions))


@pytest.mark.parametrize(
    ("entity", "target"),
    [("invoice", "issued"), ("payment", "recorded"), ("bgv_case", "adjudicated"), ("payroll_export", "finalized"),
     ("accounting_period", "closed"), ("vendor_submission", "accepted"), ("application", "hired")],
)  # fmt: skip
def test_register_action_refuses_starred_transitions(entity: str, target: str) -> None:
    with pytest.raises(ValueError, match="human-only"):
        register_action(f"e32.test_{entity}", _definition((entity, target)))
    assert f"e32.test_{entity}" not in ACTIONS


def test_custom_human_only_registration() -> None:
    catalogue.register_human_only_transitions("e32_thing", ["signed_off"])
    assert catalogue.is_human_only("e32_thing", "signed_off")
    with pytest.raises(ValueError):
        register_action("e32.sign_off", _definition(("e32_thing", "signed_off")))
    with pytest.raises((KeyError, RegistryError)):
        _ = ACTIONS["e32.sign_off"]


def test_no_registered_action_performs_a_starred_transition() -> None:
    autodiscover("app.services")
    for key, definition in ACTIONS.items():
        for entity, target in definition.performs_transitions:
            assert not catalogue.is_human_only(entity, target), (key, entity, target)
    assert catalogue.human_only_transitions()["application"] >= HUMAN_ONLY_STAGES


def test_sources_never_reference_starred_functions() -> None:
    import app.services.sla as sla_pkg
    import app.services.workflow as wf_pkg

    for package in (wf_pkg, sla_pkg):
        for path in Path(package.__file__).resolve().parent.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "decide(" not in source, path.name
            for name in STARRED:
                assert f"{name}(" not in source and f".{name}" not in source, (path.name, name)

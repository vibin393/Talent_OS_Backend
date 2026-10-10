# ruff: noqa: E501
"""Epic 63 tool contracts (static): every tool registered by owner 63 with the risk class, permissions, scope, egress /
send intent, preview and idempotency the spec names; money is never auto (``high`` except the templated pre-due
reminder, ``low`` default ``suggest``); no tool module imports a human-only (★) module; recipient-bearing tools take no
address argument. Behaviour (scope, not_found cross-tenant / cross-client, idempotency, previews, send intents, the
pre-due eligibility and holds) is exercised end to end in ``tests/agents/finance``."""

import inspect

import pytest

from app.core.agentic import tools
from app.core.hooks import autodiscover

EXPECTED: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "time.get_nudge_plan": ("read", ("timesheet:monitor",), False),
    "time.get_timesheet": ("read", ("timesheet:read",), False),
    "time.get_context": ("read", ("timesheet:read", "placement:read"), False),
    "time.list_flags": ("read", ("timesheet:read",), False),
    "time.send_timesheet_reminder": ("low", ("timesheet:monitor", "message:send_templated"), True),
    "time.save_flag_explanations": ("low", ("timesheet:read",), False),
    "time.escalate_approval": ("low", ("task:create", "timesheet:monitor"), False),
    "billing.preview_run": ("read", ("billing:read",), False),
    "billing.check_rates": ("read", ("billing:read",), False),
    "billing.list_unapproved_time": ("read", ("timesheet:read",), False),
    "billing.prepare_run_draft": ("high", ("billing:prepare", "billing:write"), False),
    "billing.flag_exception": ("low", ("note:write", "billing:read"), False),
    "receivables.get_collection_plan": ("read", ("receivables:read",), False),
    "receivables.get_client_context": ("read", ("receivables:read",), False),
    "receivables.get_activity_bodies": ("read", ("receivables:read",), False),
    "receivables.send_pre_due_reminder": ("low", ("collections:write", "message:send_templated", "email:send"), True),
    "receivables.send_reminder": ("high", ("collections:write", "email:send"), True),
    "receivables.send_dunning_message": ("high", ("collections:write", "email:send"), True),
    "receivables.send_statement": ("high", ("collections:write", "email:send"), True),
    "receivables.log_collection_note": ("low", ("collections:write",), False),
}
STAR_MODULES = (
    "app.services.billing.issue", "app.services.billing.notes", "app.services.billing.einvoice",
    "app.services.receivables.payments", "app.services.receivables.write_offs", "app.services.receivables.periods_close",
    "app.services.time.timesheets", "app.services.time.approval", "app.services.time.leave_requests",
)  # fmt: skip


@pytest.fixture(scope="module", autouse=True)
def _discovered() -> None:
    autodiscover("app.services")


def test_every_e63_tool_matches_the_spec() -> None:
    registered = {k: v for k, v in tools().items() if v.owner_epic == "63"}
    assert set(registered) == set(EXPECTED)
    for key, (risk, perms, egress) in EXPECTED.items():
        spec = registered[key]
        assert (spec.risk, spec.permissions, spec.egress) == (risk, perms, egress), key
        assert spec.version == 1
        if risk != "read":
            assert spec.preview is not None, key
        if egress:
            assert spec.send_intent is not None, key
        assert not ({"email", "address", "to", "cc", "recipient"} & set(spec.input_model.model_fields)), key


def test_money_is_never_auto() -> None:
    registered = tools()
    for key in ("billing.prepare_run_draft", "receivables.send_reminder", "receivables.send_dunning_message", "receivables.send_statement"):
        assert registered[key].risk == "high" and registered[key].default_mode == "suggest"
    pre_due = registered["receivables.send_pre_due_reminder"]
    assert (pre_due.risk, pre_due.default_mode) == ("low", "suggest")
    assert registered["receivables.get_activity_bodies"].untrusted_output == frozenset({"items.body"})
    assert "body" in registered["receivables.send_dunning_message"].untrusted_input


def test_versioned_scopes_for_staleness() -> None:
    registered = tools()
    for key in ("billing.prepare_run_draft", "receivables.send_pre_due_reminder", "receivables.send_reminder", "receivables.send_dunning_message", "receivables.send_statement"):
        assert registered[key].scope.version is not None, key


def test_no_e63_tool_module_imports_a_star_module() -> None:
    for spec in tools().values():
        if spec.owner_epic != "63":
            continue
        module = inspect.getmodule(spec.handler)
        source = inspect.getsource(module) if module is not None else ""
        assert not any(name in source for name in STAR_MODULES), spec.key
        assert spec.transition is None

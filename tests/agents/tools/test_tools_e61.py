# ruff: noqa: E501
"""Epic 61 tools: schemas (``extra="forbid"``, no reserved or recipient fields), risk / modes, previews, the consent
request's send intent (recipient from the record, templated), permission refusal, subject binding and protected-term
rejection as ``invalid``."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.agentic import get_tool
from app.core.database import tenant_session
from app.services.agent_runtime.state import ToolCtx
from tests.agents.submission.helpers import CHECK, CONSENT, CONTEXT, PREPARE, WRITE, calls, run_agent
from tests.modules.submission_packs.helpers import PackSeed

KEYS = (CONTEXT, CHECK, PREPARE, WRITE, CONSENT)
RESERVED = {"tenant_id", "actor", "owner", "permission", "scope"}


def test_schemas_are_strict_and_have_no_reserved_or_recipient_fields() -> None:
    for key in KEYS:
        spec = get_tool(key)
        assert spec.owner_epic == "61" and spec.version == 1
        assert spec.input_model.model_config.get("extra") == "forbid", key
        fields = set(spec.input_model.model_fields)
        assert not fields & RESERVED, key
        assert not {"to", "email", "recipient", "recipient_id", "address"} & fields, key
        with pytest.raises(ValidationError):
            spec.input_model.model_validate({"application_id": str(uuid.uuid4()), "unexpected": 1})


def test_risk_modes_and_flags() -> None:
    assert {get_tool(k).risk for k in (CONTEXT, CHECK)} == {"read"}
    for key in (PREPARE, WRITE, CONSENT):
        spec = get_tool(key)
        assert spec.risk == "low" and spec.default_mode == "auto" and spec.preview is not None
        assert spec.transition is None
    assert get_tool(CONSENT).egress and get_tool(CONSENT).send_intent is not None
    assert not get_tool(PREPARE).egress and not get_tool(WRITE).egress
    assert get_tool(WRITE).untrusted_input == frozenset({"text"})
    assert get_tool(CONTEXT).untrusted_output == frozenset(
        {"screening.summary", "interviews.feedback_summary", "match.explanation"}
    )


def _tctx(seed: PackSeed, db: Any) -> ToolCtx:
    from app.core.context import RequestContext
    from app.core.permissions import CATALOG, Principal, Scope

    principal = Principal(
        type="agent",
        id=seed.admin.user_id,
        tenant_id=seed.tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    ctx = RequestContext(
        tenant_id=seed.tenant_id,
        principal=principal,
        actor_kind="agent",
        actor_id=uuid.uuid4(),
        on_behalf_of=seed.admin.user_id,
    )
    return ToolCtx(
        request=ctx, db=db, actor=None, run_id=None, agent_key="submission", owner_user_id=seed.admin.user_id,
        subject=("application", seed.application_id), idempotency_key="test", execution="agent",
    )  # fmt: skip


async def test_previews_and_send_intent_resolve_from_the_record(seed: PackSeed) -> None:
    async with tenant_session(seed.tenant_id) as db:
        tctx = _tctx(seed, db)
        args = get_tool(CONSENT).input_model(application_id=seed.application_id)
        intent = await get_tool(CONSENT).send_intent(tctx, args)  # type: ignore[misc]
        assert intent.templated is True and intent.template_ref == "consent_request:client_submission"
        assert [(r.kind, r.id, r.channel) for r in intent.recipients] == [
            ("candidate", seed.candidate_id, "email")
        ]
        preview = await get_tool(CONSENT).preview(tctx, args)  # type: ignore[misc]
        assert preview.title.startswith("Send consent request to Priya Raman for ")
        prep = await get_tool(PREPARE).preview(
            tctx, get_tool(PREPARE).input_model(application_id=seed.application_id)
        )  # type: ignore[misc]
        assert prep.title.startswith("Prepare submission pack for Priya Raman")
        wp = await get_tool(WRITE).preview(
            tctx,
            get_tool(WRITE).input_model(application_id=seed.application_id, text="Priya builds pipelines."),
        )  # type: ignore[misc]
        assert wp.summary == "Priya builds pipelines." and wp.renderer == "submissions.save_write_up"


def test_permission_refusal_without_submission_draft() -> None:
    from app.core.permissions import Scope
    from app.services.agent_runtime.policy import PolicyInput, resolve

    def decide(key: str, perms: dict[str, Scope]) -> Any:
        spec = get_tool(key)
        return resolve(PolicyInput(
            global_kill=False, tenant_kill=False, agentic_on=True, agent_on=True, allow_list=frozenset({key}), tool=spec,
            args=None, effective_permissions=perms, action_policy_mode=None, config_default_mode=None,
            spec_default_mode="suggest", origin="agent",
        ))  # fmt: skip

    t = Scope.TENANT
    assert decide(PREPARE, {"application:read": t}).reason == "no_permission"
    assert decide(CONSENT, {"consent:write": t}).reason == "no_permission"
    assert decide(PREPARE, {"submission:draft": t}).mode == "auto"  # tool default after burn-in
    assert decide(CONTEXT, {"application:read": t, "submission:read": t}).mode == "auto"


async def test_subject_binding_and_write_up_rejections(seed: PackSeed) -> None:
    a = str(seed.application_id)
    ref = await run_agent(seed, [
        {"kind": "tool_calls", "calls": [[CHECK, {"application_id": str(uuid.uuid4())}]]},
        {"kind": "tool_calls", "calls": [[WRITE, {"application_id": a, "text": "Priya is a data engineer."}]]},
        {"kind": "tool_calls", "calls": [[PREPARE, {"application_id": a}]]},
        {"kind": "tool_calls", "calls": [[WRITE, {"application_id": a, "text": "Priya, a Hindu woman, is a data engineer."}]]},
        {"kind": "tool_calls", "calls": [[CHECK, {"application_id": a, "pay_rate": 1400, "bill_rate": 1500, "currency": "INR"}]]},
        {"kind": "emit_result", "result": {"outcome": "done", "summary": "tried"}},
    ])  # fmt: skip
    got = [(c.tool_key, c.outcome, c.error_code) for c in await calls(seed.tenant_id, ref.run_id)]
    assert got[0] == (CHECK, "not_found", "not_found")
    assert got[1] == (WRITE, "invalid", "pack_not_prepared")
    assert got[2][:2] == (PREPARE, "executed")
    assert got[3] == (WRITE, "invalid", "write_up_protected_attribute")
    assert got[4][:2] == (CHECK, "executed")
    check_call = (await calls(seed.tenant_id, ref.run_id))[4]
    assert "blocking" in (check_call.result_summary or "")

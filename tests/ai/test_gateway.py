"""AD-6 AI gateway: toggle -> budget -> consent -> key -> prompt -> forced tool call -> validation ->
one ai_actions row per attempt. Never raises; never touches the network (fake client)."""

import uuid
from typing import Any

import anthropic
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.models.ai import AIAction, AITenantSettings
from app.schemas.candidates import ResumeParseOutput
from app.services import ai
from tests.epic3 import FakeAI, Org3, h, member, parsed_payload

SETTINGS = "/api/v1/ai/settings"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


def _ctx(org: Org3) -> RequestContext:
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(type="user", id=org.admin.user_id, tenant_id=org.tenant_id),
    )


async def _run(
    org: Org3, *, bulk: bool = False, schema: type[BaseModel] = ResumeParseOutput
) -> tuple[ai.AIResult, AIAction]:
    async with tenant_session(org.tenant_id) as db:
        result = await ai.run(
            _ctx(org),
            db,
            "resume_parsing",
            "resume_parse",
            inputs={},
            untrusted={"document": "Priya Raman, Data Engineer"},
            output_schema=schema,
            subject=None,
            input_ref=f"test:{uuid.uuid4()}",
            bulk=bulk,
        )
        row = await db.get(AIAction, result.action_id)
        assert row is not None
    return result, row


async def test_ok_call_records_action(org3: Org3, fake_ai: FakeAI) -> None:
    result, row = await _run(org3)
    assert result.ok
    assert isinstance(result.data, ResumeParseOutput)
    assert result.data.full_name == "Priya Raman"
    assert (row.status, row.error_code) == ("ok", None)
    assert row.model == get_settings().ai_model
    assert row.prompt_version == "resume_parse.v1"
    assert (row.input_tokens, row.output_tokens) == (120, 80)
    assert row.latency_ms is not None
    assert row.consent_basis == "unchecked_pre_privacy"
    assert row.initiator_type == "user"
    (call,) = fake_ai.calls
    assert call["model"] == get_settings().ai_model
    # Forced tool_choice is rejected by current models; auto + an explicit instruction is used instead.
    assert call["tool_choice"] == {"type": "auto"}
    assert "emit_result" in call["system"]
    assert call["tools"][0]["input_schema"] == ResumeParseOutput.model_json_schema()


async def test_no_api_key_is_skipped_not_configured(org3: Org3) -> None:
    result, row = await _run(org3)
    assert not result.ok
    assert result.error_code == "not_configured"
    assert (row.status, row.error_code) == ("skipped", "not_configured")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RuntimeError("boom"), "provider_error"),
        (TimeoutError(), "timeout"),
        (anthropic.APITimeoutError(request=None), "timeout"),  # type: ignore[arg-type]
    ],
)
async def test_provider_failures_never_raise(
    org3: Org3, fake_ai: FakeAI, error: BaseException, code: str
) -> None:
    fake_ai.error = error
    result, row = await _run(org3)
    assert not result.ok
    assert result.error_code == code
    assert (row.status, row.error_code) == ("failed", code)


async def test_extra_keys_are_invalid_output(org3: Org3, fake_ai: FakeAI) -> None:
    fake_ai.payload = {**parsed_payload(), "role": "admin"}
    result, row = await _run(org3)
    assert (result.ok, result.error_code) == (False, "invalid_output")
    assert row.status == "failed"
    fake_ai.payload = {"answer": 42, "extra": 1}
    result, _ = await _run(org3, schema=_Out)
    assert result.error_code == "invalid_output"


async def test_feature_disabled_skips_without_calling(org3: Org3, fake_ai: FakeAI) -> None:
    resp = await org3.admin.client.put(
        SETTINGS, json={"features": {"resume_parsing": False}}, headers=h(org3.admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["features"]["resume_parsing"] is False
    assert resp.json()["features"]["matching"] is True
    result, row = await _run(org3)
    assert result.error_code == "feature_disabled"
    assert row.status == "skipped"
    assert fake_ai.calls == []


async def test_consent_denied_skips_without_calling(org3: Org3, fake_ai: FakeAI) -> None:
    async def deny(ctx: Any, db: Any, feature: str, subject: Any) -> ai.ConsentDecision:
        return ai.ConsentDecision(False, "consent_required")

    ai.set_consent_checker(deny)
    result, row = await _run(org3)
    assert result.error_code == "consent_missing"
    assert row.consent_basis == "consent_required"
    assert fake_ai.calls == []


async def test_budget_refuses_bulk_at_100_and_interactive_at_120(org3: Org3, fake_ai: FakeAI) -> None:
    async with tenant_session(org3.tenant_id) as db:
        db.add(AITenantSettings(tenant_id=org3.tenant_id, features={}, monthly_token_budget=1000))
        db.add(
            AIAction(
                tenant_id=org3.tenant_id,
                feature="resume_parsing",
                prompt_id="resume_parse",
                initiator_type="system",
                status="ok",
                input_tokens=1000,
                output_tokens=100,
            )
        )
    bulk, _ = await _run(org3, bulk=True)
    assert bulk.error_code == "budget_exceeded"
    fake_ai.input_tokens, fake_ai.output_tokens = 50, 0
    interactive, _ = await _run(org3)  # 1100 used -> allowed (110 %)
    assert interactive.ok
    fake_ai.input_tokens = 100
    assert (await _run(org3))[0].ok  # 1150 -> allowed, ends at 1250
    over, row = await _run(org3)  # 1250 >= 120 % -> refused
    assert over.error_code == "budget_exceeded"
    assert row.status == "skipped"


async def test_record_decision_and_list(org3: Org3, fake_ai: FakeAI) -> None:
    result, _ = await _run(org3)
    assert result.action_id is not None
    async with tenant_session(org3.tenant_id) as db:
        await ai.record_decision(_ctx(org3), db, result.action_id, "edited")
        await ai.set_subject(db, result.action_id, "candidate", org3.admin.user_id)
    async with tenant_session(org3.tenant_id) as db:
        listed = await ai.list_actions(_ctx(org3), db, "candidate", org3.admin.user_id)
        row = (await db.execute(select(AIAction).where(AIAction.id == result.action_id))).scalar_one()
    assert [a.id for a in listed] == [result.action_id]
    assert row.decision == "edited"
    assert row.decided_by == org3.admin.user_id


async def test_settings_require_ai_admin_and_are_tenant_scoped(
    app: FastAPI, org3: Org3, other3: Org3
) -> None:
    # Epic 12 (FR-63): the budget is operator-managed; tenant admins cannot change it.
    denied = await org3.admin.client.put(SETTINGS, json={"monthly_token_budget": 5000}, headers=h(org3.admin))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "budget_platform_managed"
    resp = await org3.admin.client.put(
        SETTINGS, json={"ai_processing_basis": "notice_based"}, headers=h(org3.admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["monthly_token_budget"], body["ai_processing_basis"]) == (None, "notice_based")
    assert body["configured"] is False
    other = (await other3.admin.client.get(SETTINGS)).json()
    assert other["monthly_token_budget"] is None
    assert other["ai_processing_basis"] == "consent_required"
    recruiter = await member(app, org3, "recruiter")
    try:
        assert (await recruiter.client.get(SETTINGS)).status_code == 403
    finally:
        await recruiter.client.aclose()
    bad = await org3.admin.client.put(SETTINGS, json={"features": {"telepathy": True}}, headers=h(org3.admin))
    assert bad.status_code == 422


def test_only_the_gateway_imports_anthropic() -> None:
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if re.search(r"^\s*(import anthropic|from anthropic)", p.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    # Epic 53 (AD-57): exactly the gateway and its per-step loop.
    assert sorted(offenders) == ["services/ai/gateway.py", "services/ai/loop.py"]


def test_lenient_validation_decodes_json_string_lists_only() -> None:
    from pydantic import BaseModel, ValidationError

    from app.services.ai.gateway import _validate_lenient

    class Out(BaseModel):
        name: str
        links: list[str]

    assert _validate_lenient(Out, {"name": "a", "links": '["x", "y"]'}).links == ["x", "y"]
    assert _validate_lenient(Out, {"name": "a", "links": []}).links == []
    assert _validate_lenient(Out, {"name": "a", "links": ""}).links == []
    with pytest.raises(ValidationError):
        _validate_lenient(Out, {"name": "a", "links": "not json"})
    with pytest.raises(ValidationError):
        _validate_lenient(Out, {"name": 5, "links": []})

# ruff: noqa: E501, D401
"""Per-step agent model call (Epic 53, AD-57, FR-267/FR-272). With ``gateway.py`` the only importer of ``anthropic``.

``run_agent_step(ctx, db, req)`` / ``stream_agent_step(ctx, db, req, sink)`` make exactly ONE Messages API call with
caller-supplied tools. The loop itself (deciding to call again) belongs to the runtime (``agent_runtime.executor``).
Both never raise. Order:

1. platform kill (``platform_ai_controls.global_kill_switch_at`` or ``AGENTS_GLOBALLY_DISABLED``) -> ``global_kill``;
2. tenant / platform-tenant kill switch -> ``kill_switch``;
3. features ``agentic`` and ``agent_<key>`` -> ``feature_disabled``;
4. tenant token budget (``bulk = not req.user_started``) -> ``budget_exceeded``;
5. consent checker (feature ``agent_<key>``; the privacy checker maps every feature to ``ai_processing``) -> ``consent_missing``;
6. provider: an injected client factory (tests), ``AI_PROVIDER=fake`` or a keyless non-production environment -> the
   Fake LLM; otherwise no key -> ``not_configured``;
7. redaction of every ``text`` string in user / system / tool_result content (``tool_use`` and thinking blocks untouched).

The pre-check transaction of ``db`` is committed before the model call so no transaction stays open across it
(callers pass a dedicated session -- the runtime opens one per model call). Each call writes one ``ai_actions`` row
(``kind='agent_step'``) in ``db``; the caller commits.

Request shape: ``tool_choice`` auto (never forced), ``strict`` tools, adaptive thinking, explicit ``output_config.effort``,
``cache_control`` on the system block and on the last block of the last message. Mid-run ``system`` history items are
sent as ``<system_notice>`` text inside the adjacent user turn (the Messages API has no mid-conversation system role);
the stored history stays verbatim. The server-side refusal fallback (``fallbacks="default"``) is omitted: the pinned
SDK (anthropic 1.9.0) does not accept it (recorded in the Epic 53 notes).
"""

import asyncio
import copy
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import anthropic
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.settings import get_settings
from app.models.ai import AI_FEATURES, AIAction, AITenantSettings, PlatformAIControls
from app.schemas.agentic import StepRequest, StepResult, StepUsage, ToolUse
from app.services.ai import fake_llm, gateway
from app.services.ai.model_tiers import model_for
from app.services.ai.pricing import cost_micros
from app.services.ai.redaction import redact

Sink = Callable[[str, Mapping[str, Any]], Awaitable[None]]


# ================================================================ helpers (public)
def wrap_tool_result(payload: Mapping[str, Any], untrusted_paths: frozenset[str], *, source: str) -> str:
    """JSON text of a tool result; string values at ``untrusted_paths`` (dotted, ``[]`` for list items is implicit)
    are replaced by ``wrap_untrusted(source.path, value)``."""

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, Mapping):
            return {k: walk(v, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, path) for v in node]
        if isinstance(node, str) and path in untrusted_paths and node:
            return gateway.wrap_untrusted(f"{source}.{path}", node)
        return node

    return json.dumps(walk(dict(payload), ""), ensure_ascii=False, default=str, sort_keys=True)


def tool_result_block(tool_use_id: str, text: str, *, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        block["is_error"] = True
    return block


def feature_for(agent_key: str) -> str:
    """The AI feature toggle of an agent. Test-only agents (key starting with ``_``) use the master switch only."""
    return f"agent_{agent_key}"


# ================================================================ internals
def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, Mapping):
        return dict(block)
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json", exclude_none=True))
    return {
        k: (_block_dict(v) if hasattr(v, "__dict__") and not isinstance(v, str | int | float | bool) else v)
        for k, v in vars(block).items()
        if v is not None
    }


def _redact_text(text: str, counts: dict[str, int]) -> str:
    out, found = redact(text)
    for kind, n in found.items():
        counts[kind] = counts.get(kind, 0) + n
    return out


def _redact_content(content: Any, counts: dict[str, int]) -> Any:
    if isinstance(content, str):
        return _redact_text(content, counts)
    if isinstance(content, list):
        out = []
        for block in content:
            if (
                isinstance(block, Mapping)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                out.append({**block, "text": _redact_text(block["text"], counts)})
            elif isinstance(block, Mapping) and block.get("type") == "tool_result":
                out.append({**block, "content": _redact_content(block.get("content"), counts)})
            else:
                out.append(block)  # tool_use, thinking, redacted_thinking: never altered
        return out
    return content


def _as_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [dict(b) for b in content or []]


def _wire_messages(messages: tuple[dict[str, Any], ...], counts: dict[str, int]) -> list[dict[str, Any]]:
    """API messages: system items become ``<system_notice>`` text in the adjacent user turn; consecutive same-role
    turns are merged; user/system text and tool results are redacted; assistant content is sent verbatim."""
    out: list[dict[str, Any]] = []
    for item in messages:
        role = item.get("role")
        if role == "assistant":
            out.append({"role": "assistant", "content": copy.deepcopy(item.get("content"))})
            continue
        content = _redact_content(copy.deepcopy(item.get("content")), counts)
        blocks = _as_blocks(content)
        if role == "system":
            text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            blocks = [{"type": "text", "text": f"<system_notice>{text}</system_notice>"}]
        if out and out[-1]["role"] == "user":
            out[-1]["content"] = _as_blocks(out[-1]["content"]) + blocks
        else:
            out.append({"role": "user", "content": blocks})
    if out and isinstance(out[-1]["content"], list) and out[-1]["content"]:
        last = dict(out[-1]["content"][-1])
        last["cache_control"] = {"type": "ephemeral"}
        out[-1]["content"] = [*out[-1]["content"][:-1], last]
    return out


def _initiator(ctx: RequestContext) -> tuple[str, uuid.UUID | None]:
    if ctx.actor_kind == "agent":
        return "agent", ctx.actor_id
    principal = ctx.principal
    kind = getattr(principal, "type", None)
    if kind:
        return str(kind), getattr(principal, "id", None)
    return "system", None


def _usage(response: Any) -> StepUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return StepUsage()
    return StepUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


async def _controls(db: AsyncSession) -> PlatformAIControls | None:
    return (
        await db.execute(select(PlatformAIControls).where(PlatformAIControls.id == 1))
    ).scalar_one_or_none()


def _factory() -> tuple[Callable[[str], Any] | None, str]:
    """``(client factory, api key)`` or ``(None, "")`` when not configured."""
    s = get_settings()
    if gateway._client_factory is not gateway._default_client_factory:  # injected (tests / fake_llm())
        return gateway._client_factory, s.anthropic_api_key or "fake"
    if s.ai_provider == "fake" and s.app_env != "production":
        return fake_llm.keyless_factory(), "fake"
    if not s.anthropic_api_key:
        if s.ai_fake_when_keyless and s.app_env != "production":
            return fake_llm.keyless_factory(), "fake"
        return None, ""
    return gateway._client_factory, s.anthropic_api_key


async def _mark_provider(db: AsyncSession, *, down: bool) -> None:
    try:
        if down:
            await db.execute(
                update(PlatformAIControls)
                .where(PlatformAIControls.id == 1, PlatformAIControls.provider_down_since.is_(None))
                .values(provider_down_since=datetime.now(UTC))
            )
        else:
            await db.execute(
                update(PlatformAIControls)
                .where(PlatformAIControls.id == 1, PlatformAIControls.provider_down_since.is_not(None))
                .values(provider_down_since=None)
            )
    except Exception:  # never break the step for the outage marker
        logger.exception("Could not update the provider-outage marker")


# ================================================================ the step
async def _step(ctx: RequestContext, db: AsyncSession, req: StepRequest, sink: Sink | None) -> StepResult:
    s = get_settings()
    feature = feature_for(req.agent_key)
    initiator_type, initiator_id = _initiator(ctx)
    digest = hashlib.sha256(
        json.dumps(list(req.messages), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    action = AIAction(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        feature=feature[:50],
        prompt_id=f"agent_{req.agent_key}"[:100],
        model=None,
        initiator_type=initiator_type,
        initiator_id=initiator_id,
        subject_type=req.subject[0] if req.subject else None,
        subject_id=req.subject[1] if req.subject else None,
        input_ref=f"agent_run:{req.run_id}:{req.step_no}",
        input_sha256=digest,
        status="skipped",
        kind="agent_step",
        agent_key=req.agent_key,
        agent_run_id=req.run_id,
        step_no=req.step_no,
        model_tier=req.model_tier,
    )
    try:
        action.prompt_version = gateway.load_prompt(f"agent_{req.agent_key}").ref
    except (FileNotFoundError, ValueError):
        action.prompt_version = None

    async def finish(status: str, error_code: str | None, **extra: Any) -> StepResult:
        action.status = status
        action.error_code = error_code
        try:
            db.add(action)
            await db.flush()
        except Exception:
            logger.exception("Could not record ai_action {} (agent={})", action.id, req.agent_key)
            return StepResult(ok=False, error_code=error_code or "provider_error")
        from app.services.ai import budget as budget_guard

        budget_guard.add_usage(ctx.tenant_id, (action.input_tokens or 0) + (action.output_tokens or 0))
        logger.info(
            "AI agent step {} agent={} run={} step={} status={} error={} stop={} tokens={}/{}",
            action.id, req.agent_key, req.run_id, req.step_no, status, error_code, action.stop_reason,
            action.input_tokens, action.output_tokens,
        )  # fmt: skip
        return StepResult(ok=status == "ok", action_id=action.id, error_code=error_code, **extra)

    try:
        controls = await _controls(db)
        if s.agents_globally_disabled or (
            controls is not None and controls.global_kill_switch_at is not None
        ):
            return await finish("skipped", "global_kill")
        row = (await db.execute(select(AITenantSettings))).scalar_one_or_none()
        if row is not None and (row.kill_switch_at is not None or row.platform_kill_switch_at is not None):
            return await finish("skipped", "kill_switch")
        features = gateway._features(row)
        agent_on = features.get(feature, False) if feature in AI_FEATURES else req.agent_key.startswith("_")
        if not features.get("agentic", False) or not agent_on:
            return await finish("skipped", "feature_disabled")
        budget = row.monthly_token_budget if row is not None else None
        if budget is not None and ctx.tenant_id is not None:
            from app.services.ai import budget as budget_guard

            state = await budget_guard.evaluate(db, ctx.tenant_id, budget)
            if state.all_blocked or (not req.user_started and state.bulk_blocked):
                return await finish("skipped", "budget_exceeded")
        decision = await gateway._consent_checker(ctx, db, feature, req.subject)
        action.consent_basis = decision.basis
        if not decision.allowed:
            return await finish("skipped", "consent_missing")
        factory, api_key = _factory()
        sandbox_factory = await gateway.sandbox_client_factory(ctx.tenant_id)  # Epic 66: Fake LLM in a sandbox
        if sandbox_factory is not None:
            factory, api_key = sandbox_factory, "fake"
        if factory is None:
            return await finish("skipped", "not_configured")
        counts: dict[str, int] = {}
        messages = _wire_messages(req.messages, counts)
        action.meta = {"redactions": counts} if counts else None
        model = model_for(req.model_tier, deep_opt_in=bool(row is not None and row.deep_tier_opt_in))
        action.model = model
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "strict": t.strict,
                }
                for t in req.tools
            ],
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": req.effort},
        }
        # No transaction stays open across the model call (AD-58).
        await db.commit()
    except Exception:
        logger.exception("AI agent step pre-flight failed (agent={})", req.agent_key)
        return await finish("failed", "provider_error")

    started = time.perf_counter()
    token = fake_llm.CURRENT_PROMPT.set(f"agent_{req.agent_key}")
    try:
        client = factory(api_key)
        if sink is None:
            response = await asyncio.wait_for(
                client.messages.create(**kwargs), timeout=float(s.agent_step_timeout_seconds)
            )
        else:

            async def _stream() -> Any:
                async with client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        await sink("text.delta", {"text": text})
                    return await stream.get_final_message()

            response = await asyncio.wait_for(_stream(), timeout=float(s.agent_step_timeout_seconds))
    except (TimeoutError, anthropic.APITimeoutError):
        action.latency_ms = int((time.perf_counter() - started) * 1000)
        await _mark_provider(db, down=True)
        return await finish("failed", "timeout")
    except Exception as exc:
        action.latency_ms = int((time.perf_counter() - started) * 1000)
        logger.warning("AI provider error (agent={}): {}", req.agent_key, type(exc).__name__)
        await _mark_provider(db, down=True)
        return await finish("failed", "provider_error")
    finally:
        fake_llm.CURRENT_PROMPT.reset(token)
    action.latency_ms = int((time.perf_counter() - started) * 1000)
    await _mark_provider(db, down=False)

    usage = _usage(response)
    answered_model = str(getattr(response, "model", None) or model)
    action.model = answered_model if answered_model == fake_llm.FAKE_MODEL else model
    action.input_tokens, action.output_tokens = usage.input_tokens, usage.output_tokens
    action.cache_read_tokens, action.cache_write_tokens = usage.cache_read_tokens, usage.cache_write_tokens
    action.cost_micros = cost_micros(action.model or model, usage)
    stop_reason = getattr(response, "stop_reason", None)
    action.stop_reason = stop_reason
    content = tuple(_block_dict(b) for b in (getattr(response, "content", None) or []))
    tool_uses = tuple(
        ToolUse(id=str(b.get("id")), name=str(b.get("name")), input=dict(b.get("input") or {}))
        for b in content
        if b.get("type") == "tool_use"
    )
    text = "".join(str(b.get("text") or "") for b in content if b.get("type") == "text")
    action.output = {"stop_reason": stop_reason, "tool_names": [t.name for t in tool_uses]}
    common: dict[str, Any] = {
        "content": content,
        "tool_uses": tool_uses,
        "text": text,
        "stop_reason": stop_reason,
        "usage": usage,
        "model": action.model,
    }
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        return await finish("failed", "refused", refusal_category=category, **common)
    if stop_reason == "max_tokens":
        return await finish("failed", "truncated", **common)
    return await finish("ok", None, **common)


async def run_agent_step(ctx: RequestContext, db: AsyncSession, req: StepRequest) -> StepResult:
    """One governed agent model call. Never raises (see module docstring)."""
    try:
        return await _step(ctx, db, req, None)
    except Exception:  # pragma: no cover - defensive: the contract is "never raises"
        logger.exception("AI agent step crashed (agent={})", req.agent_key)
        return StepResult(ok=False, error_code="provider_error")


async def stream_agent_step(
    ctx: RequestContext, db: AsyncSession, req: StepRequest, sink: Sink
) -> StepResult:
    """``run_agent_step`` with ``messages.stream``: text deltas go to ``sink("text.delta", {"text": ...})``; thinking
    and tool input are never streamed."""
    try:
        return await _step(ctx, db, req, sink)
    except Exception:  # pragma: no cover
        logger.exception("AI agent stream step crashed (agent={})", req.agent_key)
        return StepResult(ok=False, error_code="provider_error")


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A strict tool ``input_schema`` (``additionalProperties: false`` on every object)."""
    schema = model.model_json_schema()

    def close(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node.setdefault("additionalProperties", False)
            for v in node.values():
                close(v)
        elif isinstance(node, list):
            for v in node:
                close(v)

    close(schema)
    return schema


__all__ = [
    "feature_for",
    "output_schema",
    "run_agent_step",
    "stream_agent_step",
    "tool_result_block",
    "wrap_tool_result",
]

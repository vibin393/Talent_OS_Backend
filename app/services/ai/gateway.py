"""The single AI gateway (AD-6). The only code in the backend that imports ``anthropic``.

``run(...)`` performs, in order: feature toggle -> token budget -> consent hook -> API key check -> prompt
rendering (untrusted text wrapped in ``<untrusted_document>``) -> forced-tool model call -> pydantic
validation -> one ``ai_actions`` row per attempt. It never raises to callers: every failure becomes
``AIResult(ok=False, error_code=...)`` and the caller continues on its manual path.

Test hooks: ``set_client_factory`` (fake Anthropic client; tests never hit the network) and
``set_consent_checker`` (Epic 12 registers the real consent check).
"""

import asyncio
import dataclasses
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anthropic
from loguru import logger
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.settings import get_settings
from app.models.ai import AI_DECISIONS, AI_FEATURES, AI_FEATURES_DEFAULT_OFF, AIAction, AITenantSettings
from app.schemas.ai import AIActionOut, AIProcessingBasis, AISettingsOut, AISettingsUpdate

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
TOOL_NAME = "emit_result"
MAX_OUTPUT_TOKENS = 16000
TOOL_INSTRUCTION = (
    f"Answer only by calling the `{TOOL_NAME}` tool exactly once with the complete result. "
    "Do not reply with plain text."
)

BULK_BUDGET_LIMIT = 1.0
INTERACTIVE_BUDGET_LIMIT = 1.2
_WARN_THRESHOLDS = (0.8, 1.0)

Subject = tuple[str, uuid.UUID]


@dataclasses.dataclass(frozen=True, slots=True)
class AIResult:
    ok: bool
    data: BaseModel | None = None
    action_id: uuid.UUID | None = None
    error_code: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ConsentDecision:
    allowed: bool
    basis: str


ConsentChecker = Callable[[RequestContext, AsyncSession, str, Subject | None], Awaitable[ConsentDecision]]
ClientFactory = Callable[[str], Any]


async def _default_consent_checker(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,  # noqa: ARG001
    feature: str,  # noqa: ARG001
    subject: Subject | None,  # noqa: ARG001
) -> ConsentDecision:
    # Until Epic 12 (privacy) registers the real check: allow, and mark the basis so later audits can find
    # every pre-privacy AI processing.
    return ConsentDecision(True, "unchecked_pre_privacy")


def _default_client_factory(api_key: str) -> Any:
    s = get_settings()
    return anthropic.AsyncAnthropic(
        api_key=api_key, timeout=float(s.ai_timeout_seconds), max_retries=s.ai_max_retries
    )


_consent_checker: ConsentChecker = _default_consent_checker
# What ``set_consent_checker(None)`` restores: Epic 12 installs the privacy checker as the default.
_registered_default: ConsentChecker = _default_consent_checker
_client_factory: ClientFactory = _default_client_factory


def set_consent_checker(checker: ConsentChecker | None, *, default: bool = False) -> None:
    """Install ``checker``; ``default=True`` also makes it what ``set_consent_checker(None)`` restores
    (the privacy service registers the real checker this way at startup, replacing the allow-all stub)."""
    global _consent_checker, _registered_default  # process-wide wiring
    if default and checker is not None:
        _registered_default = checker
    _consent_checker = checker or _registered_default


def allow_all_consent_checker() -> ConsentChecker:
    """Return the pre-privacy allow-all checker (tests of features that predate consent install it
    explicitly)."""
    return _default_consent_checker


async def sandbox_client_factory(tenant_id: uuid.UUID | None) -> ClientFactory | None:
    """Epic 66 (FR-339): the keyless Fake LLM for a Sandbox Tenant regardless of keys (no spend); None otherwise or
    when a fake is already injected (tests)."""
    if tenant_id is None or _client_factory is not _default_client_factory:
        return None
    from app.core.tenancy import is_sandbox

    try:
        if not await is_sandbox(tenant_id):
            return None
    except Exception:
        logger.exception("Sandbox mode lookup failed (tenant_id={})", tenant_id)
        return None
    from app.services.ai import fake_llm

    return fake_llm.keyless_factory()


def set_client_factory(factory: ClientFactory | None) -> None:
    """Inject a fake Anthropic client factory (``api_key -> client``); ``None`` restores the real one."""
    global _client_factory  # process-wide wiring
    _client_factory = factory or _default_client_factory


# ================================================================ prompts
@dataclasses.dataclass(frozen=True, slots=True)
class Prompt:
    prompt_id: str
    version: int
    system: str
    user: str

    @property
    def ref(self) -> str:
        return f"{self.prompt_id}.v{self.version}"


_PIN_RE = re.compile(r"^(?P<id>[a-z0-9_]+)\.v(?P<v>\d+)$")
_ID_RE = re.compile(r"^[a-z0-9_]+$")
_SECTION_RE = re.compile(r"^##\s+(system|user)\s*$", re.MULTILINE | re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_OPEN_TAG_RE = re.compile(r"<(/?)(untrusted_document)", re.IGNORECASE)


def load_prompt(prompt_id: str) -> Prompt:
    """``resume_parse`` -> highest ``resume_parse.v<N>.md``; ``resume_parse.v1`` pins version 1."""
    pinned = _PIN_RE.fullmatch(prompt_id)
    if pinned:
        base, version = pinned.group("id"), int(pinned.group("v"))
    elif _ID_RE.fullmatch(prompt_id):
        versions = [
            int(m.group("v"))
            for p in PROMPTS_DIR.glob(f"{prompt_id}.v*.md")
            if (m := _PIN_RE.fullmatch(p.name.removesuffix(".md"))) and m.group("id") == prompt_id
        ]
        if not versions:
            msg = f"unknown prompt {prompt_id!r}"
            raise FileNotFoundError(msg)
        base, version = prompt_id, max(versions)
    else:
        msg = f"invalid prompt id {prompt_id!r}"
        raise ValueError(msg)
    text = (PROMPTS_DIR / f"{base}.v{version}.md").read_text(encoding="utf-8")
    parts = _SECTION_RE.split(text)
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].lower()] = parts[i + 1].strip()
    if "system" not in sections or "user" not in sections:
        msg = f"prompt {base}.v{version} needs '## system' and '## user' sections"
        raise ValueError(msg)
    return Prompt(base, version, sections["system"], sections["user"])


def escape_untrusted(text: str) -> str:
    """Neutralise any (closing) ``untrusted_document`` tag inside untrusted text."""
    return _OPEN_TAG_RE.sub(lambda m: f"&lt;{m.group(1)}{m.group(2)}", text)


def wrap_untrusted(name: str, text: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return f'<untrusted_document name="{safe_name}">\n{escape_untrusted(text)}\n</untrusted_document>'


def render(template: str, inputs: Mapping[str, str], untrusted: Mapping[str, str]) -> str:
    values: dict[str, str] = {k: str(v) for k, v in inputs.items()}
    values.update({k: wrap_untrusted(k, v) for k, v in untrusted.items()})

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            msg = f"missing prompt input {key!r}"
            raise KeyError(msg)
        return values[key]

    # One pass: substituted values are never re-scanned for placeholders.
    return _PLACEHOLDER_RE.sub(_sub, template)


def render_prompt(prompt: Prompt, inputs: Mapping[str, str], untrusted: Mapping[str, str]) -> tuple[str, str]:
    return render(prompt.system, inputs, {}), render(prompt.user, inputs, untrusted)


# ================================================================ settings / budget
def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _tenant_settings(db: AsyncSession) -> AITenantSettings | None:
    return (await db.execute(select(AITenantSettings))).scalar_one_or_none()


def _features(row: AITenantSettings | None) -> dict[str, bool]:
    stored = row.features if row is not None else {}
    return {key: bool(stored.get(key, key not in AI_FEATURES_DEFAULT_OFF)) for key in AI_FEATURES}


async def tokens_used_this_month(db: AsyncSession) -> int:
    stmt = select(
        func.coalesce(
            func.sum(func.coalesce(AIAction.input_tokens, 0) + func.coalesce(AIAction.output_tokens, 0)), 0
        )
    ).where(AIAction.created_at >= _month_start())
    return int((await db.execute(stmt)).scalar_one() or 0)


async def get_settings_out(ctx: RequestContext, db: AsyncSession) -> AISettingsOut:  # noqa: ARG001
    row = await _tenant_settings(db)
    s = get_settings()
    return AISettingsOut(
        features=_features(row),
        monthly_token_budget=row.monthly_token_budget if row is not None else None,
        # The column is a plain string limited by a DB check constraint to the AIProcessingBasis values.
        ai_processing_basis=cast(
            "AIProcessingBasis", row.ai_processing_basis if row is not None else "consent_required"
        ),
        tokens_used_this_month=await tokens_used_this_month(db),
        configured=bool(s.anthropic_api_key),
        model=s.ai_model,
    )


async def update_settings(ctx: RequestContext, db: AsyncSession, data: AISettingsUpdate) -> AISettingsOut:
    row = await _tenant_settings(db)
    if row is None:
        row = AITenantSettings(id=uuid.uuid4(), tenant_id=ctx.tenant_id, features={})
        db.add(row)
    if (
        "monthly_token_budget" in data.model_fields_set
        and data.monthly_token_budget != row.monthly_token_budget
    ):
        # Epic 12 (FR-63): the budget is set by the Platform Operator; tenant admins see it read-only.
        raise Forbidden("The AI budget is managed by the platform operator", code="budget_platform_managed")
    if data.features is not None:
        # Epic 53: ``agentic`` / ``agent_<key>`` are managed by the agents API (notice, placeholders,
        # burn-in) and are ignored here.
        changes = {str(k): bool(v) for k, v in data.features.items() if not is_agent_feature(str(k))}
        row.features = {**_features(row), **changes}
    await db.flush()
    if data.ai_processing_basis is not None:
        from app.services import privacy  # lazy: privacy depends on this module

        await privacy.set_basis(ctx, db, data.ai_processing_basis)
    return await get_settings_out(ctx, db)


def is_agent_feature(key: str) -> bool:
    """Epic 53: the agentic master switch and the per-agent toggles (managed by ``agent_runtime_service``)."""
    return key == "agentic" or key.startswith("agent_")


async def is_feature_enabled(db: AsyncSession, feature: str) -> bool:
    return _features(await _tenant_settings(db)).get(feature, False)


# ================================================================ run
def _initiator(ctx: RequestContext) -> tuple[str, uuid.UUID | None]:
    principal = ctx.principal
    kind = getattr(principal, "type", None)
    if kind:
        return str(kind), getattr(principal, "id", None)
    return "system", None


def _usage(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    return getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


_JSON_SHAPE_ERRORS = frozenset({"list_type", "dict_type", "model_type", "model_attributes_type"})


def _validate_lenient[M: BaseModel](schema: type[M], raw: Any) -> M:
    """Validate ``raw``; if a list/object field arrived as a JSON-encoded string, decode it and retry once.

    Non-strict tool calls occasionally serialise nested arrays/objects as strings (e.g. ``"links": "[]"``).
    Only that shape mismatch is repaired - every other validation error still fails the call.
    """
    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        if not isinstance(raw, dict):
            raise
        fixed = json.loads(json.dumps(raw))
        repaired = False
        for err in exc.errors():
            value = err.get("input")
            if err.get("type") not in _JSON_SHAPE_ERRORS or not isinstance(value, str):
                continue
            if not value.strip():
                decoded: Any = [] if err.get("type") == "list_type" else {}
            else:
                try:
                    decoded = json.loads(value)
                except ValueError:
                    continue
            node: Any = fixed
            loc = list(err.get("loc", ()))
            for key in loc[:-1]:
                node = node[key]
            node[loc[-1]] = decoded
            repaired = True
        if not repaired:
            raise
        return schema.model_validate(fixed)


def _tool_input(response: Any) -> Any:
    """Return the ``emit_result`` tool input, else a JSON object found in a text answer, else None."""
    blocks = getattr(response, "content", None) or []
    for block in blocks:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            return getattr(block, "input", None)
    for block in blocks:
        if getattr(block, "type", None) == "text":
            text = (getattr(block, "text", "") or "").strip()
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except ValueError:
                    continue
    return None


async def run(
    ctx: RequestContext,
    db: AsyncSession,
    feature: str,
    prompt_id: str,
    inputs: Mapping[str, str],
    untrusted: Mapping[str, str],
    output_schema: type[BaseModel],
    subject: Subject | None = None,
    input_ref: str | None = None,
    *,
    bulk: bool = False,
    model_tier: str | None = None,
) -> AIResult:
    """Run one governed model call. Never raises (see module docstring)."""
    s = get_settings()
    # Epic 53 (additive): a model tier (``fast`` for classification, e.g. the pre-screen); None = AI_MODEL.
    model_id = s.ai_model
    if model_tier is not None:
        from app.services.ai.model_tiers import model_for

        model_id = model_for(model_tier, deep_opt_in=False)  # type: ignore[arg-type]
    initiator_type, initiator_id = _initiator(ctx)
    digest = hashlib.sha256()
    for key in sorted(untrusted):
        digest.update(key.encode() + b"\0" + untrusted[key].encode("utf-8", "replace") + b"\0")
    action = AIAction(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        feature=feature,
        prompt_id=prompt_id,
        model=model_id,
        initiator_type=initiator_type,
        initiator_id=initiator_id,
        subject_type=subject[0] if subject else None,
        subject_id=subject[1] if subject else None,
        input_ref=input_ref,
        input_sha256=digest.hexdigest(),
        status="skipped",
    )

    async def _finish(status: str, error_code: str | None = None, data: BaseModel | None = None) -> AIResult:
        action.status = status
        action.error_code = error_code
        try:
            db.add(action)
            await db.flush()
        except Exception:  # the business transaction is broken; the caller's own error surfaces
            logger.exception("Could not record ai_action {} (feature={})", action.id, feature)
            return AIResult(ok=False, action_id=None, error_code=error_code or "provider_error")
        from app.services.ai import budget as budget_guard

        budget_guard.add_usage(ctx.tenant_id, (action.input_tokens or 0) + (action.output_tokens or 0))
        logger.info(
            "AI {} feature={} status={} error={} latency_ms={} tokens={}/{}",
            action.id,
            feature,
            status,
            error_code,
            action.latency_ms,
            action.input_tokens,
            action.output_tokens,
        )
        return AIResult(ok=status == "ok", data=data, action_id=action.id, error_code=error_code)

    try:
        # 1. feature toggle
        row = await _tenant_settings(db)
        if feature not in AI_FEATURES or not _features(row).get(feature, False):
            return await _finish("skipped", "feature_disabled")

        # 2. budget (Epic 12, FR-63): >=80 % alert, >=100 % bulk refused, >=120 % everything refused.
        budget = row.monthly_token_budget if row is not None else None
        if budget is not None and ctx.tenant_id is not None:
            from app.services.ai import budget as budget_guard

            state = await budget_guard.evaluate(db, ctx.tenant_id, budget)
            if state.all_blocked or (bulk and state.bulk_blocked):
                return await _finish("skipped", "budget_exceeded")

        # 3. consent
        decision = await _consent_checker(ctx, db, feature, subject)
        action.consent_basis = decision.basis
        if not decision.allowed:
            return await _finish("skipped", "consent_missing")

        # 4. configuration
        # A fake client installed (tests / keyless pre-screen) needs no key (Epic 65 follow-up).
        fake_installed = _client_factory is not _default_client_factory
        sandbox_factory = await sandbox_client_factory(ctx.tenant_id)
        if not s.anthropic_api_key and not fake_installed and sandbox_factory is None:
            return await _finish("skipped", "not_configured")

        # 5. prompt
        prompt = load_prompt(prompt_id)
        action.prompt_id = prompt.prompt_id
        action.prompt_version = prompt.ref
        # Epic 35 (NFR-24, AD-35): mask Aadhaar / individual PAN / IFSC+account before anything leaves.
        from app.services.ai.redaction import redact_inputs

        inputs, trusted_counts = redact_inputs(inputs)
        untrusted, untrusted_counts = redact_inputs(untrusted)
        redactions = {
            k: trusted_counts.get(k, 0) + untrusted_counts.get(k, 0)
            for k in {*trusted_counts, *untrusted_counts}
        }
        action.meta = {"redactions": redactions, **({"subject": "none"} if subject is None else {})}
        system, user = render_prompt(prompt, inputs, untrusted)
    except Exception:
        logger.exception("AI gateway pre-flight failed (feature={})", feature)
        return await _finish("failed", "provider_error")

    # 6. model call
    started = time.perf_counter()
    try:
        client = (sandbox_factory or _client_factory)(s.anthropic_api_key or "fake")
        response = await asyncio.wait_for(
            client.messages.create(
                model=model_id,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=f"{system}\n\n{TOOL_INSTRUCTION}",
                messages=[{"role": "user", "content": user}],
                # Current models (e.g. claude-sonnet-5-5) reject forced tool_choice, and strict/structured
                # outputs fail on our larger schemas ("compiled grammar is too large"). So: auto tool choice
                # plus an explicit instruction; the result is still validated against output_schema below.
                tools=[
                    {
                        "name": TOOL_NAME,
                        "description": "Return the result. This is the only way to answer.",
                        "input_schema": output_schema.model_json_schema(),
                    }
                ],
                tool_choice={"type": "auto"},
                timeout=float(s.ai_timeout_seconds),
            ),
            # Outer guard covering SDK retries as well.
            timeout=float(s.ai_timeout_seconds) * (s.ai_max_retries + 1) + 5,
        )
    except (TimeoutError, anthropic.APITimeoutError):
        action.latency_ms = int((time.perf_counter() - started) * 1000)
        return await _finish("failed", "timeout")
    except Exception as exc:
        action.latency_ms = int((time.perf_counter() - started) * 1000)
        logger.warning("AI provider error (feature={}): {}", feature, type(exc).__name__)
        return await _finish("failed", "provider_error")
    action.latency_ms = int((time.perf_counter() - started) * 1000)
    action.input_tokens, action.output_tokens = _usage(response)

    # 7. validation
    raw = _tool_input(response)
    try:
        if raw is None:
            raise ValueError("no tool output")
        data = _validate_lenient(output_schema, raw)
    except (ValidationError, ValueError, TypeError):
        return await _finish("failed", "invalid_output")
    action.output = data.model_dump(mode="json")
    note = getattr(data, "limitations_note", None)
    action.limitations_note = str(note)[:2000] if note else None
    return await _finish("ok", None, data)


# ================================================================ decisions / listing
async def record_decision(ctx: RequestContext, db: AsyncSession, action_id: uuid.UUID, decision: str) -> None:
    if decision not in AI_DECISIONS:
        raise ValidationFailed("Unknown AI decision", details={"decision": decision})
    row = await db.get(AIAction, action_id)
    if row is None:
        raise NotFound("AI action not found")
    row.decision = decision
    row.decided_by = getattr(ctx.principal, "id", None)
    row.decided_at = datetime.now(UTC)
    await db.flush()


async def list_actions(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject_type: str,
    subject_id: uuid.UUID | list[uuid.UUID],
) -> list[AIActionOut]:
    ids = subject_id if isinstance(subject_id, list) else [subject_id]
    stmt = (
        select(AIAction)
        .where(AIAction.subject_type == subject_type, AIAction.subject_id.in_(ids))
        .order_by(AIAction.created_at.desc(), AIAction.id)
    )
    return [AIActionOut.model_validate(r) for r in (await db.execute(stmt)).scalars()]


async def reassign_subject(
    db: AsyncSession,
    subject_type: str,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    *,
    only_ids: list[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """Point AI actions of one subject at another (candidate merge / undo). Returns moved action ids."""
    stmt = select(AIAction).where(AIAction.subject_type == subject_type)
    stmt = (
        stmt.where(AIAction.id.in_(only_ids))
        if only_ids is not None
        else stmt.where(AIAction.subject_id == from_id)
    )
    moved: list[uuid.UUID] = []
    for row in (await db.execute(stmt)).scalars():
        row.subject_id = to_id
        moved.append(row.id)
    await db.flush()
    return moved


async def set_subject(
    db: AsyncSession, action_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID
) -> None:
    """Attach a subject to an action run before the subject existed (e.g. a draft resume)."""
    row = await db.get(AIAction, action_id)
    if row is not None:
        row.subject_type = subject_type
        row.subject_id = subject_id


# ================================================================ Epic 12 helpers (basis, toggles, budget)
async def processing_basis(db: AsyncSession) -> str:
    row = await _tenant_settings(db)
    return row.ai_processing_basis if row is not None else "consent_required"


async def _settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> AITenantSettings:
    row = await _tenant_settings(db)
    if row is None:
        row = AITenantSettings(id=uuid.uuid4(), tenant_id=tenant_id, features={})
        db.add(row)
        await db.flush()
    return row


async def set_processing_basis(db: AsyncSession, tenant_id: uuid.UUID, basis: str) -> None:
    row = await _settings_row(db, tenant_id)
    row.ai_processing_basis = basis
    await db.flush()


async def features_map(db: AsyncSession) -> dict[str, bool]:
    return _features(await _tenant_settings(db))


async def set_features(
    db: AsyncSession, tenant_id: uuid.UUID, features: Mapping[str, bool]
) -> dict[str, bool]:
    row = await _settings_row(db, tenant_id)
    row.features = {**_features(row), **{str(k): bool(v) for k, v in features.items() if k in AI_FEATURES}}
    await db.flush()
    return _features(row)


async def monthly_budget(db: AsyncSession) -> int | None:
    row = await _tenant_settings(db)
    return row.monthly_token_budget if row is not None else None


async def set_monthly_budget(db: AsyncSession, tenant_id: uuid.UUID, budget: int | None) -> None:
    row = await _settings_row(db, tenant_id)
    row.monthly_token_budget = budget
    await db.flush()


async def bulk_blocked(db: AsyncSession, tenant_id: uuid.UUID | None) -> bool:
    """Whether ``bulk=True`` calls are refused right now (usage >= 100 % of the monthly budget)."""
    budget = await monthly_budget(db)
    if budget is None or tenant_id is None:
        return False
    from app.services.ai import budget as budget_guard

    return budget_guard.state_for(await budget_guard.usage(db, tenant_id), budget).bulk_blocked

# ruff: file-ignore[line-too-long, non-imperative-mood, ambiguous-unicode-character-string]
"""Schema-bound fast-tier role extraction (Epic 55, spec 0.2; injection resistance).

``extract(ctx, db, message_id)`` runs ``demand_intake_extract`` once per message through ``ai.gateway.run`` (feature
``agent_demand``, ``model_tier="fast"``, the email body + attachment texts as the untrusted ``document``) and caches
the validated result on the message (``extraction``) -- the call is idempotent.

Validation, per role:
* every field passes the P1 ``demands.intake.sanitize`` unchanged (protected attributes have no field at all);
* a field whose ``source_quote`` is not a normalised substring of the source text is **dropped** (provenance must be
  real); text values (title, location) must also occur in the source;
* skills are kept only when the label occurs in the source.

Drafts are later created from this stored result (``demand_insights``), never from model-written tool arguments.
"""

import re
import unicodedata
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.models.demand_intake import DemandIntakeMessage
from app.schemas.demand_intake import (
    ExtractedField,
    ExtractedRole,
    ExtractionOut,
    IntakeRolesExtraction,
    RoleExtraction,
)
from app.schemas.demands import IntakeExtraction

PROMPT_ID = "demand_intake_extract"
FEATURE = "agent_demand"
QUOTE_MAX = 300
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"', ord(" "): " "}
_WS = re.compile(r"\s+")
SCALAR_FIELDS = (
    "title", "min_experience_years", "max_experience_years", "positions", "location", "country", "work_mode",
    "bill_rate", "pay_rate_min", "pay_rate_max", "currency", "rate_unit", "start_date", "employment_type",
    "certifications",
)  # fmt: skip
_TEXT_VALUE_FIELDS = ("title", "location")


def normalise(text: str | None) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES).lower()
    value = value.replace(">", " ")  # quoted-reply markers
    return _WS.sub(" ", value).strip()


def quoted(quote: str | None, source_norm: str) -> bool:
    q = normalise(quote)
    return bool(q) and q in source_norm


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


async def source_text(db: AsyncSession, row: DemandIntakeMessage) -> str:
    """Body text + every extracted attachment text (stored files), capped at 60k characters."""
    from app.services import files_service

    parts: list[str] = []
    if row.body_text_file_id is not None:
        f = await files_service.get_file(db, row.body_text_file_id)
        if f is not None and f.deleted_at is None:
            parts.append((await files_service.read_file(f)).decode("utf-8", errors="replace"))
    for att in row.attachments or []:
        tid = att.get("text_file_id")
        if not tid:
            continue
        f = await files_service.get_file(db, uuid.UUID(str(tid)))
        if f is None or f.deleted_at is not None:
            continue
        text = (await files_service.read_file(f)).decode("utf-8", errors="replace")
        parts.append(f"--- Attachment: {att.get('filename')} ---\n{text}")
    return "\n\n".join(parts)[:60_000]


def _value(role: RoleExtraction, field: str) -> Any:
    item = getattr(role, field)
    return None if item is None else item.value


def _quote(role: RoleExtraction, field: str) -> str | None:
    item = getattr(role, field)
    return None if item is None or not item.source_quote else item.source_quote[:QUOTE_MAX]


def validate_role(role: RoleExtraction, index: int, source_norm: str) -> ExtractedRole | None:
    """P1 ``sanitize`` + the quote check; ``None`` when nothing usable is left."""
    from app.services.demands.intake import sanitize

    ext = IntakeExtraction(
        title=_value(role, "title"),
        mandatory_skills=list(_value(role, "mandatory_skills") or []),
        preferred_skills=list(_value(role, "preferred_skills") or []),
        **{f: _value(role, f) for f in SCALAR_FIELDS if f not in ("title", "certifications")},
        certifications=list(_value(role, "certifications") or []),
    )
    values = sanitize(ext)
    fields: dict[str, ExtractedField] = {}
    for name, value in values.items():
        if name in ("mandatory_skills", "preferred_skills"):
            continue
        quote = _quote(role, name)
        if not quoted(quote, source_norm):
            continue
        if name in _TEXT_VALUE_FIELDS and normalise(str(value)) not in source_norm:
            continue
        if name == "certifications":
            value = [c for c in value if normalise(c) in source_norm]
            if not value:
                continue
        fields[name] = ExtractedField(value=_json(value), source_quote=str(quote))
    skills: dict[str, list[str]] = {}
    for name in ("mandatory_skills", "preferred_skills"):
        quote = _quote(role, name)
        labels = (
            [s for s in values.get(name, []) if normalise(s) in source_norm]
            if quoted(quote, source_norm)
            else []
        )
        skills[name] = labels
        if labels:
            fields[name] = ExtractedField(value=labels, source_quote=str(quote))
    if not fields:
        return None
    title = fields["title"].value if "title" in fields else None
    positions = fields["positions"].value if "positions" in fields else None
    return ExtractedRole(
        role_index=index, title=title, positions=positions, fields=fields,
        mandatory_skills=skills["mandatory_skills"], preferred_skills=skills["preferred_skills"],
    )  # fmt: skip


def validate(raw: IntakeRolesExtraction, source: str, roles_max: int) -> ExtractionOut:
    source_norm = normalise(source)
    roles: list[ExtractedRole] = []
    for role in raw.roles[: max(1, min(roles_max, 5))]:
        checked = validate_role(role, len(roles), source_norm)
        if checked is not None:
            roles.append(checked)
    hints = [h.strip()[:200] for h in raw.client_hints if h and h.strip()][:5]
    return ExtractionOut(roles=roles, client_hints=hints)


async def extract(ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID) -> ExtractionOut:
    """Cached per message; raises 409 ``ai_unavailable`` when the model call fails."""
    from app.services import ai
    from app.services.ai import fake_llm
    from app.services.demand_intake import settings as settings_mod

    row = await db.get(DemandIntakeMessage, message_id)
    if row is None:
        raise NotFound("Intake message not found")
    if row.extraction is not None:
        return ExtractionOut.model_validate(row.extraction)
    if row.purged_at is not None:
        raise ConflictError("The message content was purged", code="content_purged")
    source = await source_text(db, row)
    roles_max = (await settings_mod.row(db)).roles_per_message_max
    token = fake_llm.CURRENT_PROMPT.set(
        PROMPT_ID
    )  # lets the scripted Fake LLM serve this call (tests / keyless)
    try:
        result = await ai.run(
            ctx, db, FEATURE, PROMPT_ID, inputs={"roles_max": str(roles_max)}, untrusted={"document": source},
            output_schema=IntakeRolesExtraction, input_ref=f"demand_intake_message:{row.id}", model_tier="fast",
        )  # fmt: skip
    finally:
        fake_llm.CURRENT_PROMPT.reset(token)
    if not result.ok or not isinstance(result.data, IntakeRolesExtraction):
        raise ConflictError(
            "AI extraction is unavailable right now",
            code="ai_unavailable",
            details={"reason": result.error_code},
        )
    out = validate(result.data, source, roles_max)
    row.extraction = out.model_dump(mode="json")
    row.extraction_ai_action_id = result.action_id
    await db.flush()
    return out


__all__ = [
    "FEATURE",
    "PROMPT_ID",
    "extract",
    "normalise",
    "quoted",
    "source_text",
    "validate",
    "validate_role",
]

# ruff: file-ignore[line-too-long]
"""Item bank (Epic 40 A3, FR-202): CRUD, immutable versions, per-kind validation.

Each edit inserts a new ``assessment_item_versions`` row and bumps ``current_version``; existing versions never
change (published assessments keep their frozen version ids). Archive replaces delete.

Per-kind validation uses pydantic models with ``extra='forbid'``: **no kind has a field for a test harness, a
runner spec, stdin or an expected-execution config** (a ``tests`` / ``harness`` / ``stdin`` key is a 422). The
optional ``code_output`` regex is vetted at authoring (<= 200 chars, compiles, no nested quantifiers, no
backreferences) because it is later applied with ``re.fullmatch`` to a capped response.
"""

import re
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.sequences import next_human_id
from app.core.skill_refs import SkillKey
from app.models.assessments import AssessmentItem, AssessmentItemVersion, AssessmentVersion
from app.schemas.assessments import ItemBody, ItemCreate, ItemOut, ItemVersionOut
from app.services.assessments.common import invalid, not_found, now, tenant_of, user_id

BLANK_RE = re.compile(r"\{\{blank:(\d{1,2})\}\}")
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")
_BACKREF = re.compile(r"\\[1-9]|\(\?P=")


class _F(BaseModel):
    model_config = ConfigDict(extra="forbid")


Id = Annotated[str, Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")]
Txt = Annotated[str, Field(min_length=1, max_length=2000)]


class Choice(_F):
    id: Id
    text: Txt


class ChoiceOptions(_F):
    choices: Annotated[list[Choice], Field(min_length=2, max_length=10)]


class LineOptions(_F):
    lines: Annotated[list[Choice], Field(min_length=2, max_length=60)]


class EmptyOptions(_F):
    pass


class TextNorm(_F):
    case: bool = True
    whitespace: bool = True
    punctuation: bool = True


class OutputNorm(_F):
    trailing_ws: bool = True
    blank_lines: bool = True
    case: bool = False


class TokenNorm(_F):
    token_ws: bool = True


class ChoiceKey(_F):
    correct: Id


class MultiKey(_F):
    correct: Annotated[list[Id], Field(min_length=1, max_length=10)]


class NumericKey(_F):
    value: Decimal
    tolerance_abs: Annotated[Decimal, Field(ge=0)] | None = None
    tolerance_pct: Annotated[Decimal, Field(ge=0, le=100)] | None = None


class Keyword(_F):
    term: Annotated[str, Field(min_length=1, max_length=100)]
    weight: Annotated[Decimal, Field(gt=0, le=1)]


class ShortTextKey(_F):
    accepted: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=20)] = (
        Field(default_factory=list)
    )
    normalise: TextNorm = Field(default_factory=TextNorm)
    keywords: Annotated[list[Keyword], Field(max_length=50)] | None = None
    min_keyword_score: Annotated[Decimal, Field(ge=0, le=1)] | None = None


class CodeOutputKey(_F):
    accepted: Annotated[list[Annotated[str, Field(max_length=5000)]], Field(max_length=20)] = Field(
        default_factory=list
    )
    normalise: OutputNorm = Field(default_factory=OutputNorm)
    regex: Annotated[str, Field(max_length=200)] | None = None


class Blank(_F):
    accepted: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=500)]], Field(min_length=1, max_length=20)
    ]
    normalise: TokenNorm = Field(default_factory=TokenNorm)


class CodeFillKey(_F):
    blanks: dict[Annotated[str, Field(pattern=r"^\d{1,2}$")], Blank]


class CodeOrderKey(_F):
    order: Annotated[list[Id], Field(min_length=2, max_length=60)]
    partial: Literal["lcs", "none"] = "none"


class FindBugKey(_F):
    bug_lines: Annotated[list[Annotated[int, Field(ge=1, le=2000)]], Field(min_length=1, max_length=50)]
    penalise_wrong: bool = True


class Criterion(_F):
    key: Id
    label: Annotated[str, Field(min_length=1, max_length=200)]
    max_points: Annotated[Decimal, Field(gt=0, le=1000)]
    guidance: Annotated[str, Field(max_length=2000)] | None = None


class Rubric(_F):
    criteria: Annotated[list[Criterion], Field(min_length=1, max_length=20)]


OPTIONS_MODEL: dict[str, type[_F]] = {
    "single": ChoiceOptions,
    "multi": ChoiceOptions,
    "true_false": ChoiceOptions,
    "code_order": LineOptions,
}
KEY_MODEL: dict[str, type[_F]] = {
    "single": ChoiceKey, "true_false": ChoiceKey, "multi": MultiKey, "numeric": NumericKey, "short_text": ShortTextKey,
    "code_output": CodeOutputKey, "code_fill": CodeFillKey, "code_order": CodeOrderKey, "code_find_bug": FindBugKey,
}  # fmt: skip
CODE_REQUIRED = frozenset({"code_output", "code_fill", "code_find_bug"})


def vet_regex(pattern: str) -> None:
    """Authoring-time vetting of the ``code_output`` regex (422 ``regex_unsafe``)."""
    if len(pattern) > 200:
        raise invalid("regex_unsafe", "The pattern is longer than 200 characters", "answer_key.regex")
    if _BACKREF.search(pattern):
        raise invalid("regex_unsafe", "Backreferences are not allowed", "answer_key.regex")
    if _NESTED_QUANTIFIER.search(pattern):
        raise invalid("regex_unsafe", "Nested quantifiers are not allowed", "answer_key.regex")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise invalid("regex_unsafe", f"The pattern does not compile: {exc}", "answer_key.regex") from exc


def _parse(model: type[_F], data: Any, field: str) -> Any:
    try:
        return model.model_validate(data if data is not None else {})
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err.get("loc", ()))
        path = f"{field}.{loc}" if loc else field
        raise invalid("invalid_item", f"{path}: {err.get('msg', 'invalid')}", path) from exc


def _skill_keys(keys: list[str]) -> list[str]:
    out: list[str] = []
    for raw in keys:
        try:
            key = SkillKey.parse(raw).as_str()
        except ValueError as exc:
            raise invalid("invalid_skill_key", f"Unknown skill key format: {raw}", "skill_keys") from exc
        if key not in out:
            out.append(key)
    return out


def validate_item(
    kind: str, body: ItemBody
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Validate one item version; returns normalised ``(options, answer_key, rubric)`` (JSON-ready)."""
    options_model = OPTIONS_MODEL.get(kind, EmptyOptions)
    options = _parse(options_model, body.options, "options")
    if kind in CODE_REQUIRED and not (body.code or "").strip():
        raise invalid("invalid_item", "This kind needs the code shown to the candidate", "code")
    if kind in ("code_freeform", "essay"):
        if body.answer_key:
            raise invalid(
                "invalid_item", "Free-form answers are reviewed by a person: no answer key", "answer_key"
            )
        rubric = _parse(Rubric, body.rubric, "rubric")
        keys = [c.key for c in rubric.criteria]
        if len(set(keys)) != len(keys):
            raise invalid("invalid_item", "Rubric criteria keys must be unique", "rubric.criteria")
        return options.model_dump(mode="json"), None, rubric.model_dump(mode="json")
    if body.rubric:
        raise invalid("invalid_item", "Only free-form and essay items have a rubric", "rubric")
    key = _parse(KEY_MODEL[kind], body.answer_key, "answer_key")
    if isinstance(options, ChoiceOptions):
        ids = [c.id for c in options.choices]
        if len(set(ids)) != len(ids):
            raise invalid("invalid_item", "Option ids must be unique", "options.choices")
        if kind == "true_false" and len(ids) != 2:
            raise invalid("invalid_item", "True / false items have exactly two options", "options.choices")
        correct = [key.correct] if isinstance(key, ChoiceKey) else list(key.correct)
        if not set(correct) <= set(ids):
            raise invalid(
                "invalid_item", "The correct answer must be one of the options", "answer_key.correct"
            )
    if isinstance(key, ShortTextKey) and not key.accepted and not key.keywords:
        raise invalid("invalid_item", "Give accepted answers or keywords", "answer_key.accepted")
    if isinstance(key, CodeOutputKey):
        if not key.accepted and not key.regex:
            raise invalid("invalid_item", "Give accepted outputs or a pattern", "answer_key.accepted")
        if key.regex:
            vet_regex(key.regex)
    if isinstance(key, CodeFillKey):
        markers = set(BLANK_RE.findall(body.code or ""))
        if not markers or markers != set(key.blanks):
            raise invalid(
                "invalid_item", "Every {{blank:n}} marker needs exactly one answer entry", "answer_key.blanks"
            )
    if isinstance(key, CodeOrderKey):
        line_ids = [line.id for line in options.lines]  # type: ignore[attr-defined]
        if sorted(key.order) != sorted(line_ids) or len(set(line_ids)) != len(line_ids):
            raise invalid("invalid_item", "The order must list every line exactly once", "answer_key.order")
    if isinstance(key, FindBugKey):
        n = len((body.code or "").splitlines())
        if any(line > n for line in key.bug_lines):
            raise invalid("invalid_item", "A bug line is beyond the end of the code", "answer_key.bug_lines")
    return options.model_dump(mode="json"), key.model_dump(mode="json", exclude_none=True), None


# ---------------------------------------------------------------- read
def version_out(v: AssessmentItemVersion) -> ItemVersionOut:
    return ItemVersionOut.model_validate(v)


async def _current_version(db: AsyncSession, item: AssessmentItem) -> AssessmentItemVersion:
    row = (
        await db.execute(
            select(AssessmentItemVersion).where(
                AssessmentItemVersion.item_id == item.id,
                AssessmentItemVersion.version == item.current_version,
            )
        )
    ).scalar_one()
    return row


async def _usage(db: AsyncSession, item_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not item_ids:
        return {}
    texts = list((await db.execute(select(cast(AssessmentVersion.sections, Text)))).scalars())
    return {i: sum(1 for t in texts if str(i) in t) for i in item_ids}


def _out(item: AssessmentItem, v: AssessmentItemVersion, usage: int = 0) -> ItemOut:
    return ItemOut(
        id=item.id,
        human_id=item.human_id,
        kind=item.kind,
        skill_keys=list(item.skill_keys or []),
        tags=list(item.tags or []),
        difficulty=item.difficulty,
        current_version=item.current_version,
        archived_at=item.archived_at,
        usage_count=usage,
        created_at=item.created_at,
        updated_at=item.updated_at,
        version=version_out(v),
    )


async def load(db: AsyncSession, item_id: uuid.UUID) -> AssessmentItem:
    item = await db.get(AssessmentItem, item_id)
    if item is None:
        raise not_found("Item")
    return item


async def list_items(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    q: str | None = None,
    kind: str | None = None,
    include_archived: bool = False,
    limit: int = 500,
) -> list[ItemOut]:
    stmt = select(AssessmentItem, AssessmentItemVersion).join(
        AssessmentItemVersion,
        (AssessmentItemVersion.item_id == AssessmentItem.id)
        & (AssessmentItemVersion.version == AssessmentItem.current_version),
    )
    if not include_archived:
        stmt = stmt.where(AssessmentItem.archived_at.is_(None))
    if kind:
        stmt = stmt.where(AssessmentItem.kind == kind)
    if q:
        like = f"%{q.strip()[:100].replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(
            or_(
                AssessmentItemVersion.stem.ilike(like),
                AssessmentItem.human_id.ilike(like),
                func.array_to_string(AssessmentItem.tags, " ").ilike(like),
            )
        )
    rows = (await db.execute(stmt.order_by(AssessmentItem.created_at.desc()).limit(limit))).all()
    usage = await _usage(db, [i.id for i, _ in rows])
    return [_out(i, v, usage.get(i.id, 0)) for i, v in rows]


async def get_item(ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID) -> ItemOut:  # ruff: ignore[unused-function-argument]
    item = await load(db, item_id)
    return _out(item, await _current_version(db, item), (await _usage(db, [item.id])).get(item.id, 0))


async def list_versions(ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID) -> list[ItemVersionOut]:  # ruff: ignore[unused-function-argument]
    await load(db, item_id)
    rows = (
        await db.execute(
            select(AssessmentItemVersion)
            .where(AssessmentItemVersion.item_id == item_id)
            .order_by(AssessmentItemVersion.version.desc())
        )
    ).scalars()
    return [version_out(v) for v in rows]


# ---------------------------------------------------------------- write
def _new_version(
    tenant_id: uuid.UUID, item: AssessmentItem, version: int, body: ItemBody, by: uuid.UUID | None
) -> AssessmentItemVersion:
    options, key, rubric = validate_item(item.kind, body)
    return AssessmentItemVersion(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        item_id=item.id,
        version=version,
        stem=body.stem,
        language=body.language,
        code=body.code,
        options=options,
        points=body.points,
        answer_key=key,
        rubric=rubric,
        created_by=by,
    )


async def create_item(ctx: RequestContext, db: AsyncSession, data: ItemCreate) -> ItemOut:
    tenant_id = tenant_of(db)
    item = AssessmentItem(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        human_id=await next_human_id(db, "assessment_item", "ITM", width=6),
        kind=data.kind,
        skill_keys=_skill_keys(data.skill_keys),
        tags=list(dict.fromkeys(t.strip() for t in data.tags if t.strip())),
        difficulty=data.difficulty,
        current_version=1,
        created_by=user_id(ctx),
    )
    version = _new_version(tenant_id, item, 1, data, user_id(ctx))  # validates before any insert
    db.add(item)
    await db.flush()
    db.add(version)
    await db.flush()
    await db.refresh(item)
    return _out(item, version)


async def update_item(ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID, data: ItemBody) -> ItemOut:
    item = (
        await db.execute(select(AssessmentItem).where(AssessmentItem.id == item_id).with_for_update())
    ).scalar_one_or_none()
    if item is None:
        raise not_found("Item")
    if item.archived_at is not None:
        raise invalid("item_archived", "Archived items cannot be edited")
    version = _new_version(tenant_of(db), item, item.current_version + 1, data, user_id(ctx))
    db.add(version)
    item.current_version = version.version
    item.skill_keys = _skill_keys(data.skill_keys)
    item.tags = list(dict.fromkeys(t.strip() for t in data.tags if t.strip()))
    item.difficulty = data.difficulty
    await db.flush()
    await db.refresh(item)
    return _out(item, version, (await _usage(db, [item.id])).get(item.id, 0))


async def archive_item(ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID) -> ItemOut:  # ruff: ignore[unused-function-argument]
    item = await load(db, item_id)
    if item.archived_at is None:
        item.archived_at = now()
        await db.flush()
    await db.refresh(item)
    return _out(item, await _current_version(db, item))


__all__ = [
    "BLANK_RE",
    "archive_item",
    "create_item",
    "get_item",
    "list_items",
    "list_versions",
    "load",
    "update_item",
    "validate_item",
    "vet_regex",
]

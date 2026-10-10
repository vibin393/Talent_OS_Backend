"""Pipeline stages, the §5.2 transition table (AD-4) and reason codes (story 6.1, FR-32).

``TRANSITIONS`` is data: ``(from, to) -> allowed actor kinds``. Symbolic keys:

* ``START`` -- no stage yet (a new application);
* ``custom`` -- any custom stage (they always sit between ``applied`` and ``shortlisted``);
* ``NT`` -- any non-terminal stage (terminal = hired, rejected, withdrawn).

``ai`` is in no set; ``rejected`` maps to exactly ``{user}`` (FR-65). Epic 10 may additively grant
``workflow`` on ``submitted -> interviewing`` and ``applied/custom -> custom`` -- never on anything else.
"""

import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.pipeline import ANCHORED_STAGE_KEYS, Application, PipelineReasonCode, PipelineStage
from app.schemas.pipeline import (
    ReasonCodeCreate,
    ReasonCodeOut,
    ReasonCodeUpdate,
    StageArchive,
    StageCreate,
    StageOut,
    StagesOut,
    StageTransitionOut,
    StageUpdate,
)
from app.services.pipeline.common import ActorKind, now, tenant_of

START = "__start__"
CUSTOM = "custom"
NT = "NT"
TERMINAL: frozenset[str] = frozenset({"hired", "rejected", "withdrawn"})
REASON_TARGETS: dict[str, str] = {"rejected": "reject", "withdrawn": "withdraw"}

_USER: frozenset[ActorKind] = frozenset({"user"})

TRANSITIONS: dict[tuple[str, str], frozenset[ActorKind]] = {
    (START, "applied"): frozenset({"user", "candidate", "workflow", "api_key", "app"}),  # Epic 66: +app
    # Epic 10: workflow rules may move to a custom stage; Epic 67: the Public API (app) makes the non-★ moves.
    ("applied", CUSTOM): frozenset({"user", "workflow", "app"}),
    (CUSTOM, CUSTOM): frozenset({"user", "workflow", "app"}),  # Epic 10
    (CUSTOM, "applied"): frozenset({"user", "app"}),
    ("applied", "shortlisted"): _USER,
    (CUSTOM, "shortlisted"): _USER,
    ("shortlisted", "submitted"): frozenset({"system"}),  # only the submission send path
    ("submitted", "interviewing"): frozenset({"user", "system", "workflow", "app"}),  # Epic 10 / 67
    ("interviewing", "selected"): _USER,
    ("selected", "offered"): frozenset({"system"}),
    ("offered", "hired"): frozenset({"system"}),
    ("offered", "selected"): frozenset({"system"}),
    (NT, "rejected"): _USER,  # reason_code required; never ai / workflow (FR-65)
    (NT, "withdrawn"): frozenset({"user", "candidate"}),  # reason_code required
    ("rejected", "applied"): _USER,  # reopen, reason text required
    ("withdrawn", "applied"): _USER,
}

# Default layout: anchored positions leave 1..99 for custom stages between applied and shortlisted.
DEFAULT_STAGES: tuple[tuple[str, str, str, int], ...] = (
    ("applied", "Applied", "anchored", 0),
    ("screening", "Screening", "custom", 10),
    ("shortlisted", "Shortlisted", "anchored", 100),
    ("submitted", "Submitted", "anchored", 110),
    ("interviewing", "Interviewing", "anchored", 120),
    ("selected", "Selected", "anchored", 130),
    ("offered", "Offered", "anchored", 140),
    ("hired", "Hired", "anchored", 150),
    ("rejected", "Rejected", "anchored", 160),
    ("withdrawn", "Withdrawn", "anchored", 170),
)
DEFAULT_REASONS: tuple[tuple[str, str, str], ...] = (
    ("reject", "not_qualified", "Not qualified"),
    ("reject", "compensation_mismatch", "Compensation mismatch"),
    ("reject", "notice_period", "Notice period"),
    ("reject", "location", "Location"),
    ("reject", "no_response", "No response"),
    ("reject", "client_rejected", "Client rejected"),
    ("reject", "interview_rejected", "Interview rejected"),
    ("reject", "duplicate", "Duplicate"),
    ("reject", "other", "Other"),
    ("withdraw", "not_interested", "Not interested"),
    ("withdraw", "accepted_other_offer", "Accepted another offer"),
    ("withdraw", "compensation", "Compensation"),
    ("withdraw", "personal", "Personal reasons"),
    ("withdraw", "other", "Other"),
)
MAX_CUSTOM_POSITION = 99


# ================================================================ pure transition rules
def symbol(key: str, kind: str) -> str:
    return CUSTOM if kind == "custom" else key


def allowed_actors(
    from_key: str | None, from_kind: str | None, to_key: str, to_kind: str
) -> frozenset[ActorKind] | None:
    """Return the allowed actor kinds for a move (``from_key`` None = new); None = no transition."""
    src = START if from_key is None else symbol(from_key, from_kind or "anchored")
    dst = symbol(to_key, to_kind)
    if from_key is not None and from_key == to_key:
        return None
    direct = TRANSITIONS.get((src, dst))
    if direct is not None:
        return direct
    if to_key in REASON_TARGETS and from_key is not None and from_key not in TERMINAL:
        return TRANSITIONS.get((NT, to_key))
    return None


def is_terminal(key: str) -> bool:
    return key in TERMINAL


# ================================================================ defaults
async def ensure_default_stages(db: AsyncSession, tenant_id: uuid.UUID | None = None) -> None:
    """Seed the default stages and reason codes for the session's tenant (idempotent)."""
    tid = tenant_id or tenant_of(db)
    keys = set((await db.execute(select(PipelineStage.key))).scalars())
    if not keys:
        for key, label, kind, position in DEFAULT_STAGES:
            db.add(
                PipelineStage(
                    id=uuid.uuid4(), tenant_id=tid, key=key, label=label, kind=kind, position=position
                )
            )
    else:
        for key, label, kind, position in DEFAULT_STAGES:
            if kind == "anchored" and key not in keys:
                db.add(
                    PipelineStage(
                        id=uuid.uuid4(), tenant_id=tid, key=key, label=label, kind=kind, position=position
                    )
                )
    codes = {
        (k, c) for k, c in (await db.execute(select(PipelineReasonCode.kind, PipelineReasonCode.code))).all()
    }
    if not codes:
        for kind, code, label in DEFAULT_REASONS:
            db.add(PipelineReasonCode(id=uuid.uuid4(), tenant_id=tid, kind=kind, code=code, label=label))
    await db.flush()


async def all_stages(db: AsyncSession, *, include_archived: bool = True) -> list[PipelineStage]:
    await ensure_default_stages(db)
    stmt = select(PipelineStage).order_by(PipelineStage.position, PipelineStage.key)
    if not include_archived:
        stmt = stmt.where(PipelineStage.archived_at.is_(None))
    return list((await db.execute(stmt)).scalars())


async def stage_by_key(db: AsyncSession, key: str) -> PipelineStage:
    await ensure_default_stages(db)
    row = (await db.execute(select(PipelineStage).where(PipelineStage.key == key))).scalar_one_or_none()
    if row is None:
        raise NotFound("Stage not found")
    return row


async def get_stage(db: AsyncSession, stage_id: uuid.UUID) -> PipelineStage:
    row = await db.get(PipelineStage, stage_id)
    if row is None:
        raise NotFound("Stage not found")
    return row


def stage_out(row: PipelineStage) -> StageOut:
    out = StageOut.model_validate(row)
    out.terminal = row.key in TERMINAL
    return out


# ================================================================ API: stages
async def list_stages(ctx: RequestContext, db: AsyncSession, include_transitions: bool = False) -> StagesOut:  # noqa: ARG001
    rows = await all_stages(db)
    transitions: list[StageTransitionOut] | None = None
    if include_transitions:
        live = [r for r in rows if r.archived_at is None]
        transitions = []
        for dst in live:
            actors = allowed_actors(None, None, dst.key, dst.kind)
            if actors:
                transitions.append(
                    StageTransitionOut(from_stage_id=None, to_stage_id=dst.id, actors=sorted(actors))
                )
        for src in live:
            for dst in live:
                actors = allowed_actors(src.key, src.kind, dst.key, dst.kind)
                if actors:
                    transitions.append(
                        StageTransitionOut(
                            from_stage_id=src.id,
                            to_stage_id=dst.id,
                            actors=sorted(actors),
                            reason_required=dst.key in REASON_TARGETS or is_terminal(src.key),
                        )
                    )
    return StagesOut(items=[stage_out(r) for r in rows], transitions=transitions)


def _slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"stage_{slug}"
    return slug[:40]


async def _custom_stages(db: AsyncSession) -> list[PipelineStage]:
    rows = await db.execute(
        select(PipelineStage)
        .where(PipelineStage.kind == "custom", PipelineStage.archived_at.is_(None))
        .order_by(PipelineStage.position, PipelineStage.key)
    )
    return list(rows.scalars())


async def create_stage(ctx: RequestContext, db: AsyncSession, data: StageCreate) -> StageOut:  # noqa: ARG001
    await ensure_default_stages(db)
    key = data.key or _slug(data.label)
    if key in ANCHORED_STAGE_KEYS:
        raise ConflictError("That key is reserved for an anchored stage", code="stage_exists")
    existing = (await db.execute(select(PipelineStage).where(PipelineStage.key == key))).scalar_one_or_none()
    if existing is not None:
        if existing.archived_at is None:
            raise ConflictError("A stage with this key already exists", code="stage_exists")
        existing.archived_at = None
        existing.label = data.label
        row = existing
    else:
        row = PipelineStage(
            id=uuid.uuid4(), tenant_id=tenant_of(db), key=key, label=data.label, kind="custom", position=0
        )
        db.add(row)
    customs = [c for c in await _custom_stages(db) if c.id != row.id]
    if len(customs) >= MAX_CUSTOM_POSITION:
        raise ConflictError("Too many custom stages", code="too_many_stages")
    row.position = (max((c.position for c in customs), default=0) + 1) if customs else 1
    if row.position > MAX_CUSTOM_POSITION:
        await _renumber(db, [*customs, row])
    await db.flush()
    await db.refresh(row)
    return stage_out(row)


async def _renumber(db: AsyncSession, ordered: list[PipelineStage]) -> None:
    for i, stage in enumerate(ordered, start=1):
        stage.position = i
    await db.flush()


async def update_stage(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    stage_id: uuid.UUID,
    data: StageUpdate,
) -> StageOut:
    row = await get_stage(db, stage_id)
    row.label = data.label
    await db.flush()
    await db.refresh(row)
    return stage_out(row)


async def reorder_stages(ctx: RequestContext, db: AsyncSession, stage_ids: list[uuid.UUID]) -> StagesOut:
    customs = await _custom_stages(db)
    by_id = {c.id: c for c in customs}
    if len(set(stage_ids)) != len(stage_ids) or set(stage_ids) != set(by_id):
        raise ValidationFailed(
            "Provide every active custom stage exactly once (anchored stages cannot be reordered)",
            code="invalid_stage_order",
        )
    await _renumber(db, [by_id[i] for i in stage_ids])
    return await list_stages(ctx, db)


async def archive_stage(
    ctx: RequestContext, db: AsyncSession, stage_id: uuid.UUID, data: StageArchive
) -> StageOut:
    row = await get_stage(db, stage_id)
    if row.kind == "anchored":
        raise ConflictError("Anchored stages cannot be archived or deleted", code="stage_anchored")
    if row.archived_at is not None:
        return stage_out(row)
    held = list((await db.execute(select(Application.id).where(Application.stage_id == row.id))).scalars())
    if held:
        if data.move_to_stage_id is None:
            raise ConflictError(
                "The stage still holds applications; choose a stage to move them to",
                code="stage_not_empty",
                details={"applications": len(held)},
            )
        target = await get_stage(db, data.move_to_stage_id)
        if (
            target.id == row.id
            or target.archived_at is not None
            or not (target.key == "applied" or target.kind == "custom")
        ):
            raise ValidationFailed(
                "Move to Applied or another active custom stage", code="invalid_move_target"
            )
        from app.services.pipeline.applications import system_move

        for app_id in held:
            await system_move(ctx, db, app_id, target, note=f"Stage '{row.key}' archived")
    row.archived_at = now()
    await db.flush()
    await db.refresh(row)
    return stage_out(row)


async def delete_stage(ctx: RequestContext, db: AsyncSession, stage_id: uuid.UUID) -> StageOut:
    row = await get_stage(db, stage_id)
    if row.kind == "anchored":
        raise ConflictError("Anchored stages cannot be deleted", code="stage_anchored")
    return await archive_stage(ctx, db, stage_id, StageArchive())


# ================================================================ reason codes
async def list_reason_codes(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    kind: str | None = None,
    include_archived: bool = False,
) -> list[ReasonCodeOut]:
    await ensure_default_stages(db)
    stmt = select(PipelineReasonCode).order_by(PipelineReasonCode.kind, PipelineReasonCode.label)
    if kind:
        stmt = stmt.where(PipelineReasonCode.kind == kind)
    if not include_archived:
        stmt = stmt.where(PipelineReasonCode.archived_at.is_(None))
    return [ReasonCodeOut.model_validate(r) for r in (await db.execute(stmt)).scalars()]


async def create_reason_code(ctx: RequestContext, db: AsyncSession, data: ReasonCodeCreate) -> ReasonCodeOut:  # noqa: ARG001
    await ensure_default_stages(db)
    existing = (
        await db.execute(
            select(PipelineReasonCode).where(
                PipelineReasonCode.kind == data.kind, PipelineReasonCode.code == data.code
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.archived_at is None:
            raise ConflictError("This reason code already exists", code="reason_code_exists")
        existing.archived_at = None
        existing.label = data.label
        row = existing
    else:
        row = PipelineReasonCode(
            id=uuid.uuid4(), tenant_id=tenant_of(db), kind=data.kind, code=data.code, label=data.label
        )
        db.add(row)
    await db.flush()
    await db.refresh(row)
    return ReasonCodeOut.model_validate(row)


async def _reason(db: AsyncSession, reason_id: uuid.UUID) -> PipelineReasonCode:
    row = await db.get(PipelineReasonCode, reason_id)
    if row is None:
        raise NotFound("Reason code not found")
    return row


async def update_reason_code(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    reason_id: uuid.UUID,
    data: ReasonCodeUpdate,
) -> ReasonCodeOut:
    row = await _reason(db, reason_id)
    row.label = data.label
    await db.flush()
    await db.refresh(row)
    return ReasonCodeOut.model_validate(row)


async def archive_reason_code(ctx: RequestContext, db: AsyncSession, reason_id: uuid.UUID) -> ReasonCodeOut:  # noqa: ARG001
    row = await _reason(db, reason_id)
    if row.archived_at is None:
        row.archived_at = now()
        await db.flush()
    await db.refresh(row)
    return ReasonCodeOut.model_validate(row)


async def active_reason(db: AsyncSession, kind: str, code: str) -> PipelineReasonCode | None:
    await ensure_default_stages(db)
    return (
        await db.execute(
            select(PipelineReasonCode).where(
                PipelineReasonCode.kind == kind,
                PipelineReasonCode.code == code,
                PipelineReasonCode.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def stage_map(db: AsyncSession) -> dict[uuid.UUID, PipelineStage]:
    return {s.id: s for s in await all_stages(db)}


async def counts_by_stage(db: AsyncSession, demand_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = await db.execute(
        select(Application.stage_id, func.count())
        .where(Application.demand_id == demand_id, Application.archived_at.is_(None))
        .group_by(Application.stage_id)
    )
    return {sid: int(n) for sid, n in rows.all()}


def transitions_table() -> dict[str, Any]:
    """JSON-friendly view of ``TRANSITIONS`` (docs / frontend)."""
    return {f"{a}->{b}": sorted(v) for (a, b), v in TRANSITIONS.items()}

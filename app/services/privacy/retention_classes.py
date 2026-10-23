"""Retention classes (Epic 13, AD-34): how long classes of records must be kept and when they may go.

Registry only for now: Phase 2 domains bind their tables to a class with ``register_retention_binding``
(R15); Epic 35 adds the UI, the expiry job and extra columns. Seeds: ``financial`` / ``signed_agreements`` /
``timesheets`` (96 months) and ``bgv_evidence`` (deleted after 12 months), created by migration
20261002_200_06 for existing tenants and by tenant provisioning for new ones.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.registry import Registry
from app.models.privacy import RetentionClass

DEFAULT_CLASSES: tuple[tuple[str, str, str, int, int | None], ...] = (
    ("financial", "Financial records", "document_date", 96, None),
    ("signed_agreements", "Signed agreements", "end_of_engagement", 96, None),
    ("timesheets", "Timesheets", "document_date", 96, None),
    ("bgv_evidence", "Background verification evidence", "adjudication", 0, 12),
)

# Epic 52 (A1, FR-258): Phase 3 default classes, seeded per existing tenant by migration ``20261007_316_01`` and
# insert-if-absent by ``seed_system_classes`` / ``seed_defaults`` for new tenants. Not system classes (no
# statutory floor): key -> (label, basis, anchor, period months, delete_after_months, expiry_action).
# ``marketing_logs``: campaign message logs, 2 years then delete (user decision; Epic 39 binds
# ``campaign_messages`` / ``engagement_events``). ``immigration``: 36 months after case close / last permit
# expiry (Epic 46 binds ``immigration_cases``; its anchor resolver returns the later of the two dates).
# Deviation from the spec tuple ``(…, 0, 24)``: the Epic 35 expiry engine uses ``min_retention_months`` as the
# period, so the period is 24 (``delete_after_months`` 24 kept for the P2 columns).
P3_DEFAULT_CLASSES: dict[str, tuple[str, str, str, int, int | None, str]] = {
    "marketing_logs": ("Marketing message logs", "document_date", "document_date", 24, 24, "delete"),
    "immigration": ("Immigration case records", "document_date", "document_date", 36, None, "anonymize"),
}

# Epic 53 (spec 0.3 / 0.15): Phase 4 default classes, seeded per existing tenant by migration
# ``20261008_400_04_p4_alters`` (frozen copy) and insert-if-absent by ``seed_defaults`` /
# ``seed_system_classes``. ``agent_payloads`` <- ``agent_payloads``; ``agent_logs`` <- agent tool calls /
# proposals / run results (redacted, metadata kept); ``conversation_records`` <- ``transcript_segments``;
# ``recordings`` <- recording files of ``voice_sessions`` / ``interview_captures``.
P4_DEFAULT_CLASSES: dict[str, tuple[str, str, str, int, int | None, str]] = {
    "agent_payloads": ("AI agent payloads", "document_date", "document_date", 6, 6, "delete"),
    "agent_logs": ("AI agent logs", "document_date", "document_date", 12, None, "anonymize"),
    "conversation_records": ("Conversation records", "document_date", "document_date", 6, 6, "delete"),
    "recordings": ("Call and interview recordings", "document_date", "document_date", 3, 3, "delete"),
}
# Epic 66 (AD-79, AD-82): Phase 5 default classes (must match ``CLASSES`` of migration 500_04).
P5_DEFAULT_CLASSES: dict[str, tuple[str, str, str, int, int | None, str]] = {
    "public_events": ("Public events (outbox)", "document_date", "document_date", 1, 1, "delete"),
    "api_request_logs": ("Public API request logs", "document_date", "document_date", 1, 1, "delete"),
    "vault_access_logs": ("Secret access logs", "document_date", "document_date", 12, 12, "delete"),
    "connector_payloads": ("Connector inbound payloads", "document_date", "document_date", 1, 1, "delete"),
}


@dataclasses.dataclass(frozen=True)
class RetentionClassOut:
    id: uuid.UUID
    key: str
    label: str
    basis: str
    min_retention_months: int
    delete_after_months: int | None
    is_default: bool


# Callable signatures are Epic 35's (A3); the foundation only stores them.
SubjectResolver = Callable[..., Awaitable[Any]]
AnchorResolver = Callable[..., Awaitable[Any]]
ExpireFn = Callable[..., Awaitable[Any]]


@dataclasses.dataclass(frozen=True)
class RetentionBinding:
    table: str
    class_key: str
    subject_resolver: SubjectResolver
    anchor_resolver: AnchorResolver
    expire_fn: ExpireFn
    pii_columns: tuple[str, ...] = ()


_bindings: Registry[str, RetentionBinding] = Registry("retention binding")


def register_retention_binding(
    table: str,
    class_key: str,
    *,
    subject_resolver: SubjectResolver,
    anchor_resolver: AnchorResolver,
    expire_fn: ExpireFn,
    pii_columns: Sequence[str] = (),
) -> None:
    _bindings.register(
        table,
        RetentionBinding(table, class_key, subject_resolver, anchor_resolver, expire_fn, tuple(pii_columns)),
    )


def retention_bindings() -> dict[str, RetentionBinding]:
    return dict(_bindings.view())


def _out(row: RetentionClass) -> RetentionClassOut:
    return RetentionClassOut(
        id=row.id,
        key=row.key,
        label=row.label,
        basis=row.basis,
        min_retention_months=row.min_retention_months,
        delete_after_months=row.delete_after_months,
        is_default=row.is_default,
    )


async def _seed_classes(
    db: AsyncSession, classes: dict[str, tuple[str, str, str, int, int | None, str]]
) -> None:
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    for key, (label, basis, anchor, months, delete_after, action) in classes.items():
        await db.execute(
            pg_insert(RetentionClass)
            .values(
                id=uuid.uuid4(), tenant_id=tenant_id, key=key, label=label, basis=basis,
                min_retention_months=months, delete_after_months=delete_after, is_default=True, anchor=anchor,
                expiry_action=action, is_system=False, floor_months=0,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
        )  # fmt: skip


async def seed_p3_classes(db: AsyncSession) -> None:
    """Insert-if-absent the Phase 3 default classes (Epic 52) for the session's tenant."""
    await _seed_classes(db, P3_DEFAULT_CLASSES)


async def seed_p4_classes(db: AsyncSession) -> None:
    """Insert-if-absent the Phase 4 default classes (Epic 53) for the session's tenant."""
    await _seed_classes(db, P4_DEFAULT_CLASSES)


async def seed_p5_classes(db: AsyncSession) -> None:
    """Insert-if-absent the Phase 5 default classes (Epic 66) for the session's tenant."""
    await _seed_classes(db, P5_DEFAULT_CLASSES)


async def seed_defaults(db: AsyncSession) -> None:
    """Create the default classes for the session's tenant (idempotent)."""
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    await seed_p3_classes(db)
    await seed_p4_classes(db)
    await seed_p5_classes(db)
    for key, label, basis, min_months, delete_after in DEFAULT_CLASSES:
        await db.execute(
            pg_insert(RetentionClass)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                key=key,
                label=label,
                basis=basis,
                min_retention_months=min_months,
                delete_after_months=delete_after,
                is_default=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
        )


async def get_class(db: AsyncSession, key: str) -> RetentionClassOut:
    row = (await db.execute(select(RetentionClass).where(RetentionClass.key == key))).scalar_one_or_none()
    if row is None:
        raise NotFound("Retention class not found")
    return _out(row)


async def list_classes(ctx: RequestContext, db: AsyncSession) -> list[RetentionClassOut]:  # ruff: ignore[unused-function-argument]
    rows = (await db.execute(select(RetentionClass).order_by(RetentionClass.key))).scalars()
    return [_out(r) for r in rows]


# ================================================================ Epic 35 (A3): admin CRUD
# System seeds created by migration 20261005_222_01 and on provisioning (key -> label, basis, anchor, months,
# floor, action). The period is ``min_retention_months``; a system class may be lengthened, never shortened
# below its floor.
SYSTEM_CLASSES: dict[str, tuple[str, str, str, int, int, str]] = {
    "financial": ("Financial records", "document_date", "fy_end", 96, 96, "report_only"),
    "signed_agreements": ("Signed agreements", "end_of_engagement", "signed_at", 96, 96, "delete"),
    "timesheets": ("Timesheets", "document_date", "period_end", 96, 96, "report_only"),
    "bgv_evidence": ("Background verification evidence", "adjudication", "adjudicated_at", 12, 12, "delete"),
    "timesheets_unbilled": ("Unbilled timesheets", "document_date", "period_end", 36, 36, "anonymize"),
    "worker_profile": (
        "Worker profile (bank, identity, onboarding)",
        "end_of_engagement",
        "placement_end",
        96,
        96,
        "anonymize",
    ),
}
_ANCHOR_BASIS = {
    "document_date": "document_date", "fy_end": "document_date", "period_end": "document_date",
    "adjudicated_at": "adjudication", "signed_at": "end_of_engagement", "placement_end": "end_of_engagement",
}  # fmt: skip


def class_out(row: RetentionClass) -> Any:
    from app.schemas.privacy import RetentionClassOut as RetentionClassApiOut

    return RetentionClassApiOut(
        id=row.id,
        key=row.key,
        label=row.label,
        period_months=row.min_retention_months,
        anchor=row.anchor,
        expiry_action=row.expiry_action,
        is_system=row.is_system,
        floor_months=row.floor_months,
        auto_execute=row.auto_execute,
        archived_at=row.archived_at,
    )


async def seed_system_classes(db: AsyncSession) -> None:
    """Insert-if-absent the system classes with their Epic 35 columns (provisioning)."""
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    for key, (label, basis, anchor, months, floor, action) in SYSTEM_CLASSES.items():
        await db.execute(
            pg_insert(RetentionClass)
            .values(
                id=uuid.uuid4(), tenant_id=tenant_id, key=key, label=label, basis=basis,
                min_retention_months=months, delete_after_months=None, is_default=True, anchor=anchor,
                expiry_action=action, is_system=True, floor_months=floor,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "key"],
                set_={
                    "anchor": anchor, "expiry_action": action, "is_system": True, "floor_months": floor,
                    "delete_after_months": None,
                    "min_retention_months": func.greatest(RetentionClass.min_retention_months, floor),
                },
                where=RetentionClass.is_system.is_(False),
            )
        )  # fmt: skip
    await seed_p3_classes(db)  # Epic 52: lazy seeding for tenants provisioned before Phase 3
    await seed_p4_classes(db)  # Epic 53: lazy seeding for tenants provisioned before Phase 4


async def _row(db: AsyncSession, class_id: uuid.UUID, *, lock: bool = False) -> RetentionClass:
    row = await db.get(RetentionClass, class_id, with_for_update=lock)
    if row is None:
        raise NotFound("Retention class not found")
    return row


async def list_classes_v2(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    include_archived: bool = False,
) -> list[Any]:
    await seed_system_classes(db)  # idempotent: tenants provisioned before Epic 35 / without provisioners
    stmt = select(RetentionClass).order_by(RetentionClass.is_system.desc(), RetentionClass.key)
    if not include_archived:
        stmt = stmt.where(RetentionClass.archived_at.is_(None))
    return [class_out(r) for r in (await db.execute(stmt)).scalars()]


async def create_class(ctx: RequestContext, db: AsyncSession, data: Any) -> Any:
    from app.core import audit
    from app.core.errors import ConflictError

    tenant_id = ctx.tenant_id
    exists = (await db.execute(select(RetentionClass.id).where(RetentionClass.key == data.key))).first()
    if exists is not None:
        raise ConflictError("A retention class with this key exists", code="duplicate_key")
    row = RetentionClass(
        id=uuid.uuid4(), tenant_id=tenant_id, key=data.key, label=data.label, basis=_ANCHOR_BASIS[data.anchor],
        min_retention_months=data.period_months, delete_after_months=None, is_default=False, anchor=data.anchor,
        expiry_action=data.expiry_action, is_system=False, floor_months=0, auto_execute=data.auto_execute,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    audit.record("retention.class_created", "retention_class", row.id, session=db, key=row.key)
    return class_out(row)


async def update_class(ctx: RequestContext, db: AsyncSession, class_id: uuid.UUID, data: Any) -> Any:  # ruff: ignore[unused-function-argument]
    from app.core.errors import ValidationFailed

    row = await _row(db, class_id, lock=True)
    fields = data.model_fields_set
    if "period_months" in fields and data.period_months is not None:
        from app.services.privacy.retention_overrides import check_oversight_floor

        check_oversight_floor(row.key, data.period_months)  # Epic 65 (NFR-52)
        if data.period_months < row.floor_months:
            raise ValidationFailed(
                f"The period cannot be shorter than the statutory floor of {row.floor_months} months",
                code="below_statutory_floor",
                details={
                    "floor_months": row.floor_months,
                    "fields": {"period_months": f"Minimum {row.floor_months}"},
                },
            )
        row.min_retention_months = data.period_months
    if "label" in fields and data.label is not None:
        row.label = data.label
    if "expiry_action" in fields and data.expiry_action is not None:
        if row.key == "financial" and data.expiry_action != "report_only":
            raise ValidationFailed(
                "Financial records are report-only until the immutability exception is ratified",
                code="financial_report_only",
            )
        row.expiry_action = data.expiry_action
    if "auto_execute" in fields and data.auto_execute is not None:
        row.auto_execute = data.auto_execute
    await db.flush()
    return class_out(row)


async def archive_class(ctx: RequestContext, db: AsyncSession, class_id: uuid.UUID) -> Any:  # ruff: ignore[unused-function-argument]
    from datetime import UTC, datetime

    from app.core.errors import ConflictError

    row = await _row(db, class_id, lock=True)
    if row.is_system:
        raise ConflictError("System retention classes cannot be archived", code="system_class")
    if row.key in {b.class_key for b in retention_bindings().values()}:
        raise ConflictError("Tables are bound to this class", code="class_in_use")
    row.archived_at = row.archived_at or datetime.now(UTC)
    await db.flush()
    return class_out(row)

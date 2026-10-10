"""Audit trail (AD-5, story 1.6): append-only ``audit_logs`` written in the business transaction.

* **Automatic CRUD audit.** Models inheriting ``AuditedMixin`` are audited by a ``before_flush`` listener:
  inserts (new values), updates (changed columns only, old/new) and deletes (old values) become rows with
  action ``<entity_type>.created|updated|deleted``. Only unit-of-work changes are seen -- bulk
  ``update()``/``delete()`` statements bypass the listener, so services change audited rows through the ORM.
* **Explicit events.** ``record(action, entity_type, entity_id, session=..., **data)`` adds a row for
  non-CRUD events (sign-in, export, ...) to ``session``; it is written when that session flushes/commits,
  i.e. in the same transaction. ``data`` must never carry PII (emails, names, tokens).
* **Never audited values:** ``updated_at``, any column whose name contains password/secret/token/hash, and
  a model's ``__audit_exclude__``.
* **Crypto-shredding (FR-68).** Values of a model's ``__pii_fields__`` are stored encrypted with a per-subject
  data key (``subject_keys``; the key itself is encrypted with ``AUDIT_MASTER_KEY``). ``shred()`` destroys
  the key; afterwards those values read back as ``"[erased]"``.
* **Failure semantics.** Any error while building or writing audit rows propagates, so the business change
  rolls back with it.
* **Tamper resistance.** ``talent_app`` has INSERT/SELECT only on ``audit_logs`` (migration 20260930_05).
  Tenant rows are RLS-scoped; platform rows (``tenant_id`` NULL) are visible only without tenant context.
"""

import csv
import dataclasses
import enum
import io
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Query
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Select,
    String,
    Table,
    Text,
    Uuid,
    event,
    func,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, UOWTransaction, class_mapper, mapped_column
from sqlalchemy.orm.attributes import instance_state

from app.core.context import get_context
from app.core.models import Base, UUIDPk
from app.core.pagination import Page, PageParams, paginate
from app.core.security import fernet

ACTOR_TYPES: tuple[str, ...] = (
    "user",
    "candidate",
    "platform",
    "system",
    "ai",
    "workflow",
    "api_key",
    # Phase 2 (AD-26; DB CHECK widened by 20261002_200_01): external principals and secure links.
    "client_user",
    "vendor_user",
    "secure_link",
    # Phase 4 (AD-60; DB CHECK widened by 20261008_400_01): AI agent runs (agent principal id).
    "agent",
    # Phase 5 (AD-75; DB CHECK widened by 20261009_500_01): App Principals (actor id = the Installation).
    "app",
)
ERASED = "[erased]"
MAX_EXPORT_ROWS = 50_000
# Column names containing any of these are never audited (values or diffs).
DENY_SUBSTRINGS: tuple[str, ...] = ("password", "secret", "token", "hash")
ALWAYS_EXCLUDED: frozenset[str] = frozenset({"updated_at"})
ENC = "$enc"
SUBJECT = "$subject"
# Session.info keys.
TENANT_INFO_KEY = "tenant_id"  # set by app.core.database (tenant_session)
ACTOR_INFO_KEY = "audit_actor"
_USER_AGENT_MAX = 512


# ================================================================ tables
class AuditLog(Base):
    """Append-only audit row. ``tenant_id`` is NULL only for platform events."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('user', 'candidate', 'platform', 'system', 'ai', 'workflow', 'api_key', "
            "'client_user', 'vendor_user', 'secure_link', 'agent', 'app')",
            name="actor_type",
        ),
        Index("ix_audit_logs_tenant_id_occurred_at", "tenant_id", text("occurred_at DESC")),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_actor_id", "actor_id"),
        # Epic 53 (20261008_400_04): rows caused by an agent run.
        Index("ix_audit_logs_agent_run_id", "agent_run_id"),
        # Epic 66 (20261009_500_04): rows written by an App Principal.
        Index("ix_audit_logs_app_installation_id", "app_installation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(_USER_AGENT_MAX), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Epic 53 (AD-60): the agent run that caused the row, and the proposal an approving user executed.
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    via_proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Epic 66 (AD-75, FR-331): the Installation of an ``app`` actor and the user a delegated token acts for.
    app_installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    on_behalf_of_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class SubjectKey(Base):
    """Per-subject data key for PII in audit rows; ``key_encrypted`` is NULL once shredded."""

    __tablename__ = "subject_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    subject_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    key_encrypted: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


_SUBJECT_KEYS: Table = SubjectKey.__table__  # type: ignore[assignment]


# ================================================================ helpers
def _now() -> datetime:
    return datetime.now(UTC)


def _sync(session: AsyncSession | Session) -> Session:
    return session.sync_session if isinstance(session, AsyncSession) else session


def jsonable(value: Any) -> Any:
    """JSON-compatible copy of ``value`` (uuid/datetime/Decimal/enum -> str-ish)."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((jsonable(v) for v in value), key=str)
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return "<binary>"
    return str(value)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _session_tenant(session: Session) -> uuid.UUID | None:
    return _uuid_or_none(session.info.get(TENANT_INFO_KEY))


def set_actor(session: AsyncSession | Session, actor_type: str, actor_id: uuid.UUID | None) -> None:
    """Attribute this session's audit rows to ``actor`` (e.g. during sign-in, before a principal exists)."""
    if actor_type not in ACTOR_TYPES:
        msg = f"unknown actor type {actor_type!r}"
        raise ValueError(msg)
    _sync(session).info[ACTOR_INFO_KEY] = (actor_type, actor_id)


def _actor(session: Session) -> tuple[str, uuid.UUID | None]:
    override = session.info.get(ACTOR_INFO_KEY)
    if override is not None:
        return override[0], override[1]
    principal = get_context().principal
    kind = getattr(principal, "type", None)
    if kind in ACTOR_TYPES:
        return str(kind), _uuid_or_none(getattr(principal, "id", None))
    return "system", None


def _new_row(
    session: Session,
    *,
    tenant_id: uuid.UUID | None,
    action: str,
    entity_type: str | None,
    entity_id: Any,
    changes: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    actor: tuple[str, uuid.UUID | None] | None = None,
) -> AuditLog:
    ctx = get_context()
    actor_type, actor_id = actor if actor is not None else _actor(session)
    if actor_type not in ACTOR_TYPES:
        msg = f"unknown actor type {actor_type!r}"
        raise ValueError(msg)
    # Epic 66 (FR-331): App Principals carry their Installation and, for delegated tokens, the acting user.
    principal = ctx.principal
    app_principal = getattr(principal, "type", None) == "app"
    return AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        occurred_at=_now(),
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        changes=changes or None,
        data=data or None,
        ip=ctx.ip,
        user_agent=ctx.user_agent[:_USER_AGENT_MAX] if ctx.user_agent else None,
        request_id=ctx.request_id,
        agent_run_id=ctx.agent_run_id,
        via_proposal_id=ctx.proposal_id,
        agent_key=ctx.agent_key,
        app_installation_id=getattr(principal, "app_installation_id", None) if app_principal else None,
        on_behalf_of_user_id=getattr(principal, "acting_user_id", None) if app_principal else None,
    )


# ================================================================ explicit events
def record(
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | str | None = None,
    *,
    session: AsyncSession | Session,
    **data: Any,
) -> None:
    """Record a non-CRUD audit event in ``session``'s transaction (written on its next flush/commit).

    ``data`` may override ``actor_type`` / ``actor_id`` (e.g. during sign-in) and ``tenant_id``; the tenant
    defaults to the session's tenant (NULL in a platform session = platform event) and an explicit one must
    match it (RLS would refuse the row anyway).
    """
    sync = _sync(session)
    session_tenant = _session_tenant(sync)
    if "tenant_id" in data:
        wanted = _uuid_or_none(data.pop("tenant_id"))
        if wanted != session_tenant:
            msg = (
                f"audit event {action} for tenant {wanted} recorded in a session for tenant {session_tenant}"
            )
            raise ValueError(msg)
    actor: tuple[str, uuid.UUID | None] | None = None
    if "actor_type" in data or "actor_id" in data:
        default_type, default_id = _actor(sync)
        actor = (
            str(data.pop("actor_type", default_type) or default_type),
            _uuid_or_none(data.pop("actor_id", default_id)),
        )
    row = _new_row(
        sync,
        tenant_id=session_tenant,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        data={k: jsonable(v) for k, v in data.items()},
        actor=actor,
    )
    sync.add(row)
    logger.bind(audit_action=action).info(
        "AUDIT {} entity={}:{} actor={}:{} tenant_id={}",
        action,
        entity_type,
        row.entity_id,
        row.actor_type,
        row.actor_id,
        row.tenant_id,
    )


# ================================================================ automatic CRUD audit (listener)
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def entity_type_of(cls: type) -> str:
    explicit = getattr(cls, "__audit_entity__", None)
    return str(explicit) if explicit else _CAMEL.sub("_", cls.__name__).lower()


def _is_audited(obj: object) -> bool:
    return bool(getattr(type(obj), "__audited__", False))


def is_excluded(cls: type, field: str) -> bool:
    name = field.lower()
    return (
        name in ALWAYS_EXCLUDED
        or any(part in name for part in DENY_SUBSTRINGS)
        or field in getattr(cls, "__audit_exclude__", ())
    )


def _column_keys(obj: object) -> list[str]:
    return [prop.key for prop in class_mapper(type(obj)).column_attrs if not is_excluded(type(obj), prop.key)]


def _entity_id(obj: object) -> Any:
    attr = getattr(type(obj), "__audit_entity_id__", "id")
    return getattr(obj, attr, None)


def _insert_changes(obj: object) -> dict[str, Any]:
    state = instance_state(obj)
    out: dict[str, Any] = {}
    for key in _column_keys(obj):
        value = state.dict.get(key)
        if value is not None:
            out[key] = {"old": None, "new": jsonable(value)}
    return out


def _update_changes(obj: object) -> dict[str, Any]:
    state = instance_state(obj)
    out: dict[str, Any] = {}
    for key in _column_keys(obj):
        history = state.attrs[key].history
        if not history.has_changes():
            continue
        old = history.deleted[0] if history.deleted else None
        new = history.added[0] if history.added else None
        if old == new:
            continue
        out[key] = {"old": jsonable(old), "new": jsonable(new)}
    return out


def _delete_changes(obj: object) -> dict[str, Any]:
    state = instance_state(obj)
    out: dict[str, Any] = {}
    for key in _column_keys(obj):
        if key in state.dict and state.dict[key] is not None:
            out[key] = {"old": jsonable(state.dict[key]), "new": None}
    return out


class _SubjectKeys:
    """Per-flush cache of subject data keys (created on first use)."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.cache: dict[tuple[uuid.UUID, str, uuid.UUID], Fernet | None] = {}

    def get(self, tenant_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID) -> Fernet | None:
        """Return the subject's data key, or None once shredded."""
        cache_key = (tenant_id, subject_type, subject_id)
        if cache_key not in self.cache:
            self.cache[cache_key] = self._load_or_create(*cache_key)
        return self.cache[cache_key]

    def _select(self, tenant_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID) -> Any:
        table = _SUBJECT_KEYS
        return self.session.execute(
            select(table.c.key_encrypted, table.c.destroyed_at).where(
                table.c.tenant_id == tenant_id,
                table.c.subject_type == subject_type,
                table.c.subject_id == subject_id,
            )
        ).first()

    def _load_or_create(
        self, tenant_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID
    ) -> Fernet | None:
        row = self._select(tenant_id, subject_type, subject_id)
        if row is None:
            self.session.execute(
                pg_insert(_SUBJECT_KEYS)
                .values(
                    tenant_id=tenant_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    key_encrypted=fernet("audit").encrypt(Fernet.generate_key()).decode("ascii"),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "subject_type", "subject_id"])
            )
            row = self._select(tenant_id, subject_type, subject_id)
        if row is None or row.destroyed_at is not None or not row.key_encrypted:
            return None
        return Fernet(fernet("audit").decrypt(row.key_encrypted.encode("ascii")))


def _protect(
    keys: _SubjectKeys, obj: object, tenant_id: uuid.UUID | None, changes: dict[str, Any]
) -> dict[str, Any]:
    pii = [f for f in getattr(type(obj), "__pii_fields__", ()) if f in changes]
    if not pii:
        return changes
    subject_type = str(getattr(type(obj), "__pii_subject__", None) or entity_type_of(type(obj)))
    subject_id = _uuid_or_none(_entity_id(obj))
    if tenant_id is None or subject_id is None:
        msg = f"cannot encrypt PII of {type(obj).__name__} without tenant and subject id"
        raise RuntimeError(msg)
    key = keys.get(tenant_id, subject_type, subject_id)
    subject = f"{subject_type}:{subject_id}"
    for field in pii:
        diff = changes[field]
        for side in ("old", "new"):
            if diff.get(side) is None:
                continue
            if key is None:
                diff[side] = ERASED
            else:
                token = key.encrypt(json.dumps(diff[side]).encode("utf-8")).decode("ascii")
                diff[side] = {ENC: token, SUBJECT: subject}
    return changes


@event.listens_for(Session, "before_flush")
def _audit_before_flush(session: Session, _flush_context: UOWTransaction, _instances: object) -> None:
    entries: list[tuple[object, str, dict[str, Any]]] = []
    for obj in list(session.new):
        if _is_audited(obj):
            if isinstance(obj, UUIDPk) and getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()  # the entity id must be known before the INSERT
            entries.append((obj, "created", _insert_changes(obj)))
    for obj in list(session.dirty):
        if _is_audited(obj) and session.is_modified(obj, include_collections=False):
            changes = _update_changes(obj)
            if changes:
                entries.append((obj, "updated", changes))
    for obj in list(session.deleted):
        if _is_audited(obj):
            entries.append((obj, "deleted", _delete_changes(obj)))
    if not entries:
        return
    keys = _SubjectKeys(session)
    session_tenant = _session_tenant(session)
    for obj, verb, changes in entries:
        tenant_id = _uuid_or_none(getattr(obj, "tenant_id", None)) or session_tenant
        entity_type = entity_type_of(type(obj))
        session.add(
            _new_row(
                session,
                tenant_id=tenant_id,
                action=f"{entity_type}.{verb}",
                entity_type=entity_type,
                entity_id=_entity_id(obj),
                changes=_protect(keys, obj, tenant_id, changes),
            )
        )


# ================================================================ crypto-shredding
async def shred(subject_type: str, subject_id: uuid.UUID, *, session: AsyncSession) -> None:
    """Destroy the subject's data key (tenant of ``session``): its audited PII then reads ``[erased]``.

    Idempotent. A subject without a key gets a destroyed placeholder so later rows are erased too.
    """
    tenant_id = _session_tenant(session.sync_session)
    if tenant_id is None:
        msg = "shred() needs a tenant session"
        raise RuntimeError(msg)
    now = _now()
    table = _SUBJECT_KEYS
    stmt = pg_insert(table).values(
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_id=subject_id,
        key_encrypted=None,
        destroyed_at=now,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["tenant_id", "subject_type", "subject_id"],
            set_={"key_encrypted": None, "destroyed_at": func.coalesce(table.c.destroyed_at, now)},
        )
    )
    record("audit.subject_shredded", subject_type, subject_id, session=session)


# ================================================================ reading (decryption)
async def _load_keys(
    db: AsyncSession, subjects: Iterable[tuple[str, uuid.UUID]]
) -> dict[tuple[str, uuid.UUID], Fernet | None]:
    wanted = set(subjects)
    if not wanted:
        return {}
    table = _SUBJECT_KEYS
    rows = await db.execute(
        select(table.c.subject_type, table.c.subject_id, table.c.key_encrypted, table.c.destroyed_at).where(
            tuple_(table.c.subject_type, table.c.subject_id).in_(list(wanted))
        )
    )
    keys: dict[tuple[str, uuid.UUID], Fernet | None] = dict.fromkeys(wanted)
    master = fernet("audit")
    for subject_type, subject_id, key_encrypted, destroyed_at in rows.tuples():
        if destroyed_at is None and key_encrypted:
            try:
                keys[subject_type, subject_id] = Fernet(master.decrypt(key_encrypted.encode("ascii")))
            except InvalidToken:
                logger.error("Audit subject key not decryptable with AUDIT_MASTER_KEY ({})", subject_type)
    return keys


def _parse_subject(value: Any) -> tuple[str, uuid.UUID] | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    subject_type, _, raw_id = value.rpartition(":")
    try:
        return subject_type, uuid.UUID(raw_id)
    except ValueError:
        return None


def _encrypted_subjects(changes: Mapping[str, Any] | None) -> set[tuple[str, uuid.UUID]]:
    out: set[tuple[str, uuid.UUID]] = set()
    for diff in (changes or {}).values():
        if isinstance(diff, Mapping):
            for side in diff.values():
                if isinstance(side, Mapping) and ENC in side:
                    subject = _parse_subject(side.get(SUBJECT))
                    if subject is not None:
                        out.add(subject)
    return out


def _reveal(value: Any, keys: Mapping[tuple[str, uuid.UUID], Fernet | None]) -> Any:
    if not (isinstance(value, Mapping) and ENC in value):
        return value
    subject = _parse_subject(value.get(SUBJECT))
    key = keys.get(subject) if subject is not None else None
    if key is None:
        return ERASED
    try:
        return json.loads(key.decrypt(str(value[ENC]).encode("ascii")))
    except (InvalidToken, ValueError):
        return ERASED


def decrypt_changes(
    changes: Mapping[str, Any] | None, keys: Mapping[tuple[str, uuid.UUID], Fernet | None]
) -> dict[str, Any] | None:
    if changes is None:
        return None
    return {
        field: ({side: _reveal(v, keys) for side, v in diff.items()} if isinstance(diff, Mapping) else diff)
        for field, diff in changes.items()
    }


async def reveal_rows(db: AsyncSession, rows: Sequence[AuditLog]) -> list["AuditLogOut"]:
    subjects: set[tuple[str, uuid.UUID]] = set()
    for row in rows:
        subjects |= _encrypted_subjects(row.changes)
    keys = await _load_keys(db, subjects)
    return [
        AuditLogOut(
            id=row.id,
            occurred_at=row.occurred_at,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            changes=decrypt_changes(row.changes, keys),
            data=row.data,
            ip=row.ip,
            user_agent=row.user_agent,
            request_id=row.request_id,
        )
        for row in rows
    ]


# ================================================================ search / export (tenant admin API)
class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    occurred_at: datetime
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    entity_type: str | None
    entity_id: str | None
    changes: dict[str, Any] | None
    data: dict[str, Any] | None
    ip: str | None
    user_agent: str | None
    request_id: str | None


@dataclasses.dataclass(frozen=True)
class AuditFilters:
    """Query filters: ``Annotated[AuditFilters, Depends()]``. ``action`` matches as a prefix."""

    entity_type: Annotated[str | None, Query(max_length=64)] = None
    entity_id: Annotated[str | None, Query(max_length=64)] = None
    actor_id: Annotated[uuid.UUID | None, Query()] = None
    action: Annotated[str | None, Query(max_length=100, description="Action prefix, e.g. 'user.'")] = None
    # Epic 35 (A6): filter by actor type (user, client_user, vendor_user, ai, workflow, ...).
    actor_type: Annotated[str | None, Query(max_length=16)] = None
    from_: Annotated[datetime | None, Query(alias="from")] = None
    to: Annotated[datetime | None, Query()] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            ("from" if k == "from_" else k): jsonable(v)
            for k, v in dataclasses.asdict(self).items()
            if v is not None
        }


def _like_prefix(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filtered(filters: AuditFilters) -> Select[tuple[AuditLog]]:
    stmt = select(AuditLog)
    if filters.entity_type:
        stmt = stmt.where(AuditLog.entity_type == filters.entity_type)
    if filters.entity_id:
        stmt = stmt.where(AuditLog.entity_id == filters.entity_id)
    if filters.actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == filters.actor_id)
    if filters.actor_type:
        stmt = stmt.where(AuditLog.actor_type == filters.actor_type)
    if filters.action:
        stmt = stmt.where(AuditLog.action.like(_like_prefix(filters.action), escape="\\"))
    if filters.from_ is not None:
        stmt = stmt.where(AuditLog.occurred_at >= filters.from_)
    if filters.to is not None:
        stmt = stmt.where(AuditLog.occurred_at < filters.to)
    # RLS already confines a tenant session to its rows; the explicit predicate also hides platform rows.
    return stmt.where(AuditLog.tenant_id.is_not(None)).order_by(
        AuditLog.occurred_at.desc(), AuditLog.id.desc()
    )


async def search(db: AsyncSession, filters: AuditFilters, params: PageParams) -> Page[AuditLogOut]:
    """Newest-first page of the session tenant's audit rows, PII decrypted (or ``[erased]``)."""
    page = await paginate(db, _filtered(filters), params)
    return Page[AuditLogOut](
        items=await reveal_rows(db, page.items), total=page.total, page=page.page, page_size=page.page_size
    )


CSV_COLUMNS: tuple[str, ...] = (
    "occurred_at",
    "action",
    "actor_type",
    "actor_id",
    "entity_type",
    "entity_id",
    "changes",
    "data",
    "ip",
    "user_agent",
    "request_id",
    "id",
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    text = (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        if isinstance(value, dict | list)
        else str(jsonable(value))
    )
    # Spreadsheet formula injection guard.
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


@dataclasses.dataclass(frozen=True, slots=True)
class CsvExport:
    content: str
    rows: int
    truncated: bool


async def export_csv(db: AsyncSession, filters: AuditFilters) -> CsvExport:
    """CSV of at most ``MAX_EXPORT_ROWS`` matching rows (newest first); the export itself is audited."""
    result = await db.execute(_filtered(filters).limit(MAX_EXPORT_ROWS + 1))
    rows = list(result.scalars().all())
    truncated = len(rows) > MAX_EXPORT_ROWS
    rows = rows[:MAX_EXPORT_ROWS]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for item in await reveal_rows(db, rows):
        values = item.model_dump()
        writer.writerow([_csv_cell(values[c]) for c in CSV_COLUMNS])
    record(
        "audit.exported",
        "audit_log",
        None,
        session=db,
        rows=len(rows),
        truncated=truncated,
        filters=filters.as_dict(),
    )
    return CsvExport(content=out.getvalue(), rows=len(rows), truncated=truncated)


def audited_models() -> list[type[Any]]:
    """Every mapped class audited by the listener (for tests / docs)."""
    return [m.class_ for m in Base.registry.mappers if getattr(m.class_, "__audited__", False)]


# ================================================================ Epic 35 registries (FR-183, A6)
# Owners register from their ``hooks.py`` (or the module that performs the action); core never imports them.
ActorResolver = Callable[[Any, Sequence[uuid.UUID]], Awaitable[Mapping[uuid.UUID, str]]]
TimelineSource = Callable[[Any, uuid.UUID], Awaitable[Iterable[tuple[str, Any]]]]
AuditSummarizer = Callable[[Any], str | None]

_AUDIT_ACTIONS: dict[str, str] = {}
_ACTOR_RESOLVERS: dict[str, ActorResolver] = {}
_TIMELINE_SOURCES: dict[str, list[TimelineSource]] = {}
_SUMMARIZERS: dict[str, AuditSummarizer] = {}


def register_audit_action(key: str, description: str) -> None:
    """Declare an explicit (non-CRUD) audit action key; idempotent, last description wins."""
    _AUDIT_ACTIONS[key] = description


def audit_actions() -> dict[str, str]:
    """Return the registered explicit action catalogue ``{key: description}``."""
    return dict(_AUDIT_ACTIONS)


def register_actor_resolver(kind: str, fn: ActorResolver) -> None:
    """``fn(db, ids) -> {id: display name}`` for audit rows of ``actor_type == kind`` (e.g. client_user)."""
    _ACTOR_RESOLVERS[kind] = fn


def actor_resolver(kind: str) -> ActorResolver | None:
    return _ACTOR_RESOLVERS.get(kind)


def register_timeline_source(subject_type: str, fn: TimelineSource) -> None:
    """``fn(db, subject_id) -> [(entity_type, entity_id), ...]`` -- related entities merged into the subject's
    record timeline. Registering the same function twice is a no-op."""
    sources = _TIMELINE_SOURCES.setdefault(subject_type, [])
    if fn not in sources:
        sources.append(fn)


def timeline_sources(subject_type: str) -> list[TimelineSource]:
    return list(_TIMELINE_SOURCES.get(subject_type, ()))


def register_audit_summarizer(action_or_entity: str, fn: AuditSummarizer) -> None:
    """``fn(row) -> one-line summary`` keyed by an exact action (``invoice.issued``) or an entity type."""
    _SUMMARIZERS[action_or_entity] = fn


def audit_summarizer(action: str, entity_type: str | None) -> AuditSummarizer | None:
    return _SUMMARIZERS.get(action) or (_SUMMARIZERS.get(entity_type) if entity_type else None)


register_audit_action("retention.expired", "Records past their retention class were deleted or anonymised")
register_audit_action("golive.applied", "A go-live import batch was applied")


__all__ = [
    "ACTOR_TYPES",
    "ERASED",
    "AuditFilters",
    "AuditLog",
    "AuditLogOut",
    "SubjectKey",
    "record",
    "set_actor",
    "shred",
]

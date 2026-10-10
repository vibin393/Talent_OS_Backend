"""Activity timeline service (Epic 2, story 2.3): notes, logged calls/meetings, system entries.

* **Entities.** Any module can put a timeline on its records: ``register_entity("client", checker,
  plural="clients")`` where ``checker(ctx, db, entity_id, write) -> bool`` says whether the caller may see
  (``write=False``) or change the record. Unknown types and records the caller cannot see are 404.
* **Notes** (``note|call|meeting``) need ``note:write`` plus read access to the record. Only the author may
  edit, within 24 h of creation (403 ``edit_window_closed`` after). Private notes are visible to their
  author and to holders of ``activity:read_private``.
* **Mentions** use ``@[Display Name](user:<uuid>)`` markup; every id must be a user of the tenant (422
  ``invalid_mention``). Each new mention emits ``UserMentioned`` after commit.
* **System entries** (``add_system_entry``) are written by other services (never PII in the body).
* **Timeline**: newest first by ``(occurred_at, id)`` with an opaque cursor.
"""

import base64
import binascii
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import acting_user_id
from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.events import emit, subscribe
from app.core.permissions import Principal, check
from app.events.activity import ActivityLogged, UserMentioned
from app.models.activity import USER_KINDS, Activity

# Epic 33: captured email replies and SMS / WhatsApp messages are system entries too.
SYSTEM_ENTRY_KINDS = ("system", "email", "email_reply", "sms", "whatsapp", "call")  # Epic 76: +call
from app.models.identity import User
from app.schemas.activity import ActivityCreate, ActivityOut, ActivityUpdate, Participant, TimelinePage

EDIT_WINDOW = timedelta(hours=24)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
NOTE_WRITE = "note:write"
READ_PRIVATE = "activity:read_private"
MENTION_RE = re.compile(
    r"@\[(?P<name>[^\]\n]{1,200})\]\(user:(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)"
)

EntityChecker = Callable[[RequestContext, AsyncSession, uuid.UUID, bool], Awaitable[bool]]

_checkers: dict[str, EntityChecker] = {}
_plurals: dict[str, str] = {}  # URL segment -> entity type


def _now() -> datetime:
    return datetime.now(UTC)


# ================================================================ entity registry
def register_entity(entity_type: str, checker: EntityChecker, *, plural: str | None = None) -> None:
    """Enable timelines/notes for ``entity_type`` (URL segment ``plural``, default ``<entity_type>s``)."""
    _checkers[entity_type] = checker
    _plurals[plural or f"{entity_type}s"] = entity_type
    _plurals.setdefault(entity_type, entity_type)


def registered_entities() -> dict[str, str]:
    return dict(_plurals)


def resolve_entity_type(segment: str) -> str:
    entity_type = _plurals.get(segment)
    if entity_type is None or entity_type not in _checkers:
        raise NotFound("Resource not found")
    return entity_type


async def require_entity(
    ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID, *, write: bool = False
) -> None:
    """404 unless ``entity_type`` is registered and its checker lets the caller see (or change) the record."""
    checker = _checkers.get(entity_type)
    if checker is None or not await checker(ctx, db, entity_id, write):
        raise NotFound("Resource not found")


# Epic 67: the Public API calls these with App Principals and API keys (author = the acting user or NULL).
CALLER_TYPES = frozenset({"user", "app", "api_key"})


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in CALLER_TYPES or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


# ================================================================ mentions
def parse_mentions(body: str) -> list[uuid.UUID]:
    """User ids mentioned with ``@[Name](user:<uuid>)`` markup, in order, without duplicates."""
    return list(dict.fromkeys(uuid.UUID(m.group("id")) for m in MENTION_RE.finditer(body)))


def _invalid_mention(ids: Iterable[uuid.UUID]) -> ValidationFailed:
    missing = sorted(str(i) for i in ids)
    message = f"Unknown mentioned users: {', '.join(missing)}"
    return ValidationFailed(
        message,
        code="invalid_mention",
        details={"fields": [{"field": "body", "message": message, "type": "invalid_mention"}]},
    )


async def _validate_mentions(db: AsyncSession, body: str) -> list[uuid.UUID]:
    ids = parse_mentions(body)
    if ids:
        found = set((await db.execute(select(User.id).where(User.id.in_(ids)))).scalars())  # RLS: tenant only
        if missing := set(ids) - found:
            raise _invalid_mention(missing)
    return ids


def _emit_mentions(db: AsyncSession, activity: Activity, user_ids: Iterable[uuid.UUID]) -> None:
    for user_id in user_ids:
        if user_id == activity.author_id:
            continue
        emit(
            db,
            UserMentioned(
                tenant_id=activity.tenant_id,
                activity_id=activity.id,
                entity_type=activity.entity_type,
                entity_id=activity.entity_id,
                mentioned_user_id=user_id,
                author_id=activity.author_id,
            ),
        )


def _log_mention(event: UserMentioned) -> None:
    # Epic 10 replaces this with a notification; ids only (no PII).
    logger.info(
        "UserMentioned activity_id={} mentioned_user_id={} tenant_id={}",
        event.activity_id,
        event.mentioned_user_id,
        event.tenant_id,
    )


subscribe(UserMentioned, _log_mention)


# ================================================================ output
def _can_read_private(principal: Principal) -> bool:
    return principal.has(READ_PRIVATE)


def _can_edit(principal: Principal | None, activity: Activity, now: datetime) -> bool:
    return (
        principal is not None
        and activity.author_id is not None
        and activity.author_id == acting_user_id(principal)
        and activity.kind in USER_KINDS
        and now - activity.created_at <= EDIT_WINDOW
        and principal.has(NOTE_WRITE)
    )


async def _author_names(db: AsyncSession, activities: Sequence[Activity]) -> dict[uuid.UUID, str]:
    ids = {a.author_id for a in activities if a.author_id is not None}
    if not ids:
        return {}
    rows = await db.execute(select(User.id, User.name).where(User.id.in_(ids)))
    return dict(rows.tuples().all())


async def _out(
    db: AsyncSession, principal: Principal | None, activities: Sequence[Activity]
) -> list[ActivityOut]:
    names = await _author_names(db, activities)
    now = _now()
    return [
        ActivityOut(
            id=a.id,
            entity_type=a.entity_type,
            entity_id=a.entity_id,
            kind=a.kind,
            body=a.body,
            occurred_at=a.occurred_at,
            author_id=a.author_id,
            author_name=names.get(a.author_id) if a.author_id else None,
            is_private=a.is_private,
            participants=[Participant.model_validate(p) for p in a.participants],
            mentions=list(a.mentions),
            data=a.data,
            edited_at=a.edited_at,
            created_at=a.created_at,
            can_edit=_can_edit(principal, a, now),
        )
        for a in activities
    ]


# ================================================================ timeline
def encode_cursor(activity: Activity) -> str:
    raw = f"{activity.occurred_at.isoformat()}|{activity.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, _, raw_id = base64.urlsafe_b64decode(padded.encode()).decode().partition("|")
        occurred = datetime.fromisoformat(stamp)
        if occurred.tzinfo is None:
            raise ValueError
        return occurred, uuid.UUID(raw_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValidationFailed("Invalid cursor", code="invalid_cursor") from exc


# ---------------------------------------------------------------- related refs (Epic 15)
# Providers add entity refs whose activities also show on a record's timeline (e.g. the source Lead of an
# Opportunity / Client). Registered by domains from ``hooks.py``; this module never imports them.
RelatedRefsProvider = Callable[[RequestContext, AsyncSession, str, uuid.UUID], Awaitable[list[tuple[str, uuid.UUID]]]]
_related_providers: dict[str, RelatedRefsProvider] = {}
LOGGED_KINDS = ("note", "call", "meeting", "email")


def register_related_refs_provider(name: str, provider: RelatedRefsProvider) -> None:
    _related_providers[name] = provider


async def _merged_refs(
    ctx: RequestContext,
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    also: Sequence[tuple[str, uuid.UUID]],
) -> list[tuple[str, uuid.UUID]]:
    refs: list[tuple[str, uuid.UUID]] = [(entity_type, entity_id)]
    extra = list(also)
    for provider in list(_related_providers.values()):
        extra.extend(await provider(ctx, db, entity_type, entity_id))
    for ref_type, ref_id in extra:
        if (ref_type, ref_id) in refs:
            continue
        checker = _checkers.get(ref_type)
        if checker is not None and await checker(ctx, db, ref_id, False):  # permission-checked via the registry
            refs.append((ref_type, ref_id))
    return refs


def _emit_logged(db: AsyncSession, activity: Activity, actor_kind: str) -> None:
    if activity.kind not in LOGGED_KINDS:
        return
    emit(
        db,
        ActivityLogged(
            tenant_id=activity.tenant_id,
            activity_id=activity.id,
            entity_type=activity.entity_type,
            entity_id=activity.entity_id,
            kind=activity.kind,
            actor_kind=actor_kind,
        ),
    )


def _visible(principal: Principal) -> Any:
    if _can_read_private(principal):
        return None
    return or_(Activity.is_private.is_(False), Activity.author_id == acting_user_id(principal))


async def timeline(
    ctx: RequestContext,
    db: AsyncSession,
    entity_segment: str,
    entity_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    also: Sequence[tuple[str, uuid.UUID]] | None = None,
) -> TimelinePage:
    principal = _principal(ctx)
    entity_type = resolve_entity_type(entity_segment)
    await require_entity(ctx, db, entity_type, entity_id)
    limit = max(1, min(limit, MAX_LIMIT))
    refs = await _merged_refs(ctx, db, entity_type, entity_id, also or [])
    stmt = select(Activity).where(
        or_(*(and_(Activity.entity_type == t, Activity.entity_id == i) for t, i in refs))
    )
    if (visible := _visible(principal)) is not None:
        stmt = stmt.where(visible)
    if cursor:
        occurred, last_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Activity.occurred_at < occurred, and_(Activity.occurred_at == occurred, Activity.id < last_id)
            )
        )
    rows = list(
        (await db.execute(stmt.order_by(Activity.occurred_at.desc(), Activity.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    page, more = rows[:limit], len(rows) > limit
    return TimelinePage(
        items=await _out(db, principal, page), next_cursor=encode_cursor(page[-1]) if more else None
    )


async def count_visible(ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> int:
    """Timeline size as the caller sees it (private notes of others excluded)."""
    principal = _principal(ctx)
    stmt = (
        select(func.count())
        .select_from(Activity)
        .where(Activity.entity_type == entity_type, Activity.entity_id == entity_id)
    )
    if (visible := _visible(principal)) is not None:
        stmt = stmt.where(visible)
    return int((await db.execute(stmt)).scalar_one())


# ================================================================ notes
async def add_activity(
    ctx: RequestContext, db: AsyncSession, entity_segment: str, entity_id: uuid.UUID, data: ActivityCreate
) -> ActivityOut:
    """Add a note / logged call or meeting to a record the caller can see."""
    principal = _principal(ctx)
    check(principal, NOTE_WRITE)
    entity_type = resolve_entity_type(entity_segment)
    await require_entity(ctx, db, entity_type, entity_id)
    mentions = await _validate_mentions(db, data.body)
    now = _now()
    activity = Activity(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        entity_type=entity_type,
        entity_id=entity_id,
        kind=data.kind,
        body=data.body,
        occurred_at=data.occurred_at or now,
        author_id=acting_user_id(principal),
        is_private=data.is_private,
        participants=[p.model_dump(mode="json", exclude_none=True) for p in data.participants],
        mentions=mentions,
        created_at=now,
    )
    db.add(activity)
    await db.flush()
    _emit_mentions(db, activity, mentions)
    _emit_logged(db, activity, principal.type)
    (out,) = await _out(db, principal, [activity])
    return out


async def update_activity(
    ctx: RequestContext, db: AsyncSession, activity_id: uuid.UUID, data: ActivityUpdate
) -> ActivityOut:
    """Author-only edit within ``EDIT_WINDOW`` of creation."""
    principal = _principal(ctx)
    activity = await db.get(Activity, activity_id, with_for_update=True)
    if activity is None:
        raise NotFound("Activity not found")
    await require_entity(ctx, db, activity.entity_type, activity.entity_id)
    if activity.is_private and activity.author_id != principal.id and not _can_read_private(principal):
        raise NotFound("Activity not found")
    check(principal, NOTE_WRITE)
    if activity.author_id != principal.id or activity.kind not in USER_KINDS:
        raise Forbidden("Only the author can edit this entry", code="not_author")
    now = _now()
    if now - activity.created_at > EDIT_WINDOW:
        raise Forbidden("Notes can only be edited within 24 hours", code="edit_window_closed")
    sent = data.model_fields_set
    if "body" in sent and data.body is not None and data.body != activity.body:
        mentions = await _validate_mentions(db, data.body)
        new_mentions = [m for m in mentions if m not in set(activity.mentions)]
        activity.body = data.body
        activity.mentions = mentions
        activity.edited_at = now
        _emit_mentions(db, activity, new_mentions)
    if "is_private" in sent and data.is_private is not None and data.is_private != activity.is_private:
        activity.is_private = data.is_private
        activity.edited_at = now
    await db.flush()
    (out,) = await _out(db, principal, [activity])
    return out


# ================================================================ API for other services
async def add_system_entry(
    ctx: RequestContext,
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    body: str,
    data: dict[str, Any] | None = None,
    *,
    kind: str = "system",
    occurred_at: datetime | None = None,
    meta: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Append a system (or ``email``) entry to a record's timeline. ``body`` and ``data`` must not carry PII.

    ``db`` must be the caller's tenant session. The author is the calling user, if any.
    """
    if kind not in SYSTEM_ENTRY_KINDS:
        msg = f"add_system_entry kind must be one of {SYSTEM_ENTRY_KINDS}, not {kind!r}"
        raise ValueError(msg)
    tenant_id = db.sync_session.info.get("tenant_id")
    if tenant_id is None:
        msg = "add_system_entry needs a tenant session"
        raise RuntimeError(msg)
    principal = ctx.principal
    author = principal.id if isinstance(principal, Principal) and principal.type == "user" else None
    now = _now()
    activity = Activity(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(str(tenant_id)),
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind,
        body=body[:2000],
        occurred_at=occurred_at or now,
        author_id=author,
        data=({**(meta or {}), **(data or {})}) if (meta or data) else None,
        created_at=now,
    )
    db.add(activity)
    await db.flush()
    if kind in LOGGED_KINDS:
        _emit_logged(db, activity, "user" if author else "system")
    return activity.id


async def reassign_entity(
    ctx: RequestContext,  # noqa: ARG001 - uniform service signature
    db: AsyncSession,
    entity_type: str,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Move every activity of ``from_id`` to ``to_id`` (record merge). Returns the moved ids (audited)."""
    rows = (
        await db.execute(
            select(Activity).where(Activity.entity_type == entity_type, Activity.entity_id == from_id)
        )
    ).scalars()
    moved: list[uuid.UUID] = []
    for activity in rows:
        activity.entity_id = to_id
        moved.append(activity.id)
    await db.flush()
    return moved


async def reassign_activities(
    ctx: RequestContext,  # noqa: ARG001 - uniform service signature
    db: AsyncSession,
    entity_type: str,
    activity_ids: Sequence[uuid.UUID],
    to_id: uuid.UUID,
) -> None:
    """Move exactly ``activity_ids`` (of ``entity_type``) to ``to_id`` (undo of a merge)."""
    if not activity_ids:
        return
    rows = (
        await db.execute(
            select(Activity).where(Activity.entity_type == entity_type, Activity.id.in_(list(activity_ids)))
        )
    ).scalars()
    for activity in rows:
        activity.entity_id = to_id
    await db.flush()

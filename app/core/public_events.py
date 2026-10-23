# ruff: file-ignore[line-too-long]
"""Public Event registry (Epic 66, AD-79, FR-334/350). Pure plumbing: imports nothing from ``app.services`` or ``app.models``.

``register_public_event(spec)`` maps an internal domain event class to a versioned Public Event type and subscribes an
in-transaction outbox writer (``core.events.subscribe_in_tx``): for every committed domain event of ``spec.source``,
``spec.build(event)`` returns the thin ``data`` (``None`` = do not publish) and the writer ``session.add``s one
``outbox_events`` row in the same transaction. A plain after-commit subscriber kicks the relay for the tenant.

Cycle-free wiring: core never imports ``app.models``; the owner package (``app.services.public_events``) installs
* ``set_outbox_row_factory(fn)`` -- ``fn(draft: OutboxDraft) -> ORM row`` (an ``OutboxEvent``), and
* ``set_dispatch_kicker(fn)`` -- ``fn(tenant_id)`` called after commit (enqueues ``public_events.dispatch``, deduplicated).

Subject type: ``spec.subject_type`` names the row's subject; the special value ``"*"`` takes it from the built data's
``subject_type`` field (``subject.erased``, whose subject is a candidate or a worker).
"""

import dataclasses
import re
import types
import typing
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core import events
from app.core.actors import actor_from_ctx
from app.core.context import get_context

TYPE_RE = re.compile(r"^[a-z_]+\.[a-z_]+$")
DYNAMIC_SUBJECT = "*"
_TENANT_INFO_KEY = "tenant_id"  # app.core.database.TENANT_INFO_KEY

PII_FIELD_DENYLIST = {"name", "first_name", "last_name", "full_name", "email", "phone", "mobile", "address", "dob", "date_of_birth",
                      "pan", "aadhaar", "passport", "bank", "account_number", "ifsc", "salary", "ctc", "compensation", "rate",
                      "bill_rate", "pay_rate", "notes", "body", "text", "comment", "resume", "feedback", "reason"}  # fmt: skip


@dataclasses.dataclass(frozen=True, slots=True)
class PublicEventSpec:
    type: str  # ^[a-z_]+\.[a-z_]+$  e.g. "application.stage_changed"
    version: int  # data schema version (1)
    source: type  # type: ignore[valid-type]  # internal domain event class
    subject_type: str  # "application"
    subject_id: Callable[[Any], uuid.UUID]  # from the domain event
    data_model: type[  # type: ignore[valid-type]
        BaseModel
    ]  # extra="forbid"; only ids, enums, status values, timestamps, changed field NAMES
    build: Callable[[Any], BaseModel | None]  # pure: domain event -> data (None = do not publish)
    scopes: tuple[str, ...]  # scopes a subscriber needs (any one), e.g. ("applications:read",)
    visibility: Literal["public", "internal"] = (
        "public"  # internal: connector triggers only, never webhooks or docs
    )
    owner_epic: str = "66"
    description: str = ""
    example: Mapping[str, Any] = dataclasses.field(default_factory=dict)  # fmt: skip


@dataclasses.dataclass(frozen=True, slots=True)
class OutboxDraft:
    """What the writer hands the row factory (one ``outbox_events`` row)."""

    tenant_id: uuid.UUID
    event_type: str
    event_version: int
    visibility: str
    subject_type: str
    subject_id: uuid.UUID
    occurred_at: datetime
    data: dict[str, Any]
    source_event: str
    actor_kind: str | None
    app_installation_id: uuid.UUID | None


OutboxRowFactory = Callable[[OutboxDraft], Any]
DispatchKicker = Callable[[uuid.UUID], None]

_specs: dict[tuple[str, int], PublicEventSpec] = {}
_writers: dict[tuple[str, int], tuple[Callable[[Session, Any], None], Callable[[Any], None]]] = {}
_row_factory: OutboxRowFactory | None = None
_kicker: DispatchKicker | None = None


def set_outbox_row_factory(fn: OutboxRowFactory | None) -> None:
    global _row_factory  # process-wide hook installed by app.services.public_events
    _row_factory = fn


def set_dispatch_kicker(fn: DispatchKicker | None) -> None:
    global _kicker  # process-wide hook installed by app.services.public_events
    _kicker = fn


def get_dispatch_kicker() -> DispatchKicker | None:
    return _kicker


def _build(spec: PublicEventSpec, domain_event: Any) -> BaseModel | None:
    data = spec.build(domain_event)
    if data is None:
        return None
    if not isinstance(data, spec.data_model):
        data = spec.data_model.model_validate(data.model_dump() if isinstance(data, BaseModel) else data)  # type: ignore[attr-defined]
    return data


def _context_actor() -> tuple[str | None, uuid.UUID | None]:
    ctx = get_context()
    principal = ctx.principal
    installation_id = None
    if getattr(principal, "type", None) == "app":
        installation_id = getattr(principal, "app_installation_id", None) or getattr(principal, "id", None)
    try:
        kind: str | None = actor_from_ctx(ctx).kind
    except Exception:  # pragma: no cover - defensive: never fail a commit on attribution
        kind = None
    return kind, installation_id


def draft_for(spec: PublicEventSpec, domain_event: Any) -> OutboxDraft | None:
    """Return the outbox row draft for ``domain_event`` (``None`` when ``build`` returns ``None``). Pure apart from the context."""
    data = _build(spec, domain_event)
    if data is None:
        return None
    dumped = data.model_dump(mode="json", exclude_unset=True)
    subject_type = spec.subject_type
    if subject_type == DYNAMIC_SUBJECT:
        subject_type = str(dumped.get("subject_type") or "")
        if not subject_type:
            msg = f"public event {spec.type}: dynamic subject without data.subject_type"
            raise ValueError(msg)
    occurred_at = getattr(domain_event, "occurred_at", None)
    if not isinstance(occurred_at, datetime):
        occurred_at = datetime.now(UTC)
    elif occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    actor_kind, installation_id = _context_actor()
    return OutboxDraft(
        tenant_id=uuid.UUID(str(domain_event.tenant_id)),
        event_type=spec.type,
        event_version=spec.version,
        visibility=spec.visibility,
        subject_type=subject_type,
        subject_id=uuid.UUID(str(spec.subject_id(domain_event))),
        occurred_at=occurred_at,
        data=dumped,
        source_event=type(domain_event).__name__[:80],
        actor_kind=actor_kind,
        app_installation_id=installation_id,
    )


def _outbox_writer(spec: PublicEventSpec) -> Callable[[Session, Any], None]:
    def writer(session: Session, domain_event: Any) -> None:
        draft = draft_for(spec, domain_event)
        if draft is None:
            return
        session_tenant = session.info.get(_TENANT_INFO_KEY)
        if not session_tenant:
            # A platform session cannot write tenant rows (RLS); never abort a non-tenant commit for the outbox.
            logger.error("public event {} emitted outside a tenant session; not written", spec.type)
            return
        if str(session_tenant) != str(draft.tenant_id):
            msg = f"public event {spec.type}: event tenant differs from the session tenant"
            raise ValueError(msg)
        if _row_factory is None:
            msg = "outbox row factory not installed (app.services.public_events hooks not loaded)"
            raise RuntimeError(msg)
        session.add(_row_factory(draft))

    writer.__qualname__ = f"outbox_writer[{spec.type}@v{spec.version}]"
    return writer


def _after_commit_kick(spec: PublicEventSpec) -> Callable[[Any], None]:
    def kick(domain_event: Any) -> None:
        if _kicker is None:
            return
        try:
            if _build(spec, domain_event) is None:
                return
        except Exception:
            return  # the writer already failed the commit; nothing was written
        _kicker(uuid.UUID(str(domain_event.tenant_id)))

    kick.__qualname__ = f"outbox_kick[{spec.type}@v{spec.version}]"
    return kick


def register_public_event(spec: PublicEventSpec) -> None:
    """Register ``spec`` and subscribe its in-transaction outbox writer (and the after-commit relay kick)."""
    if not TYPE_RE.match(spec.type):
        msg = f"public event type {spec.type!r} must match {TYPE_RE.pattern}"
        raise ValueError(msg)
    key = (spec.type, spec.version)
    if key in _specs:
        msg = f"duplicate public event {spec.type} v{spec.version}"
        raise ValueError(msg)
    writer, kick = _outbox_writer(spec), _after_commit_kick(spec)
    _specs[key] = spec
    _writers[key] = (writer, kick)
    events.subscribe_in_tx(spec.source, writer)
    events.subscribe(spec.source, kick)


def unregister_public_event(event_type: str, version: int = 1) -> None:
    """Remove a registration (tests only)."""
    spec = _specs.pop((event_type, version), None)
    pair = _writers.pop((event_type, version), None)
    if spec is not None and pair is not None:
        events.unsubscribe_in_tx(spec.source, pair[0])
        events.unsubscribe(spec.source, pair[1])


def all_specs(*, include_internal: bool = False) -> list[PublicEventSpec]:
    """Every registered (type, version), sorted."""
    return [s for k, s in sorted(_specs.items()) if include_internal or s.visibility == "public"]


def catalogue(*, include_internal: bool = False) -> Mapping[str, PublicEventSpec]:
    """Public Event type -> its latest registered version."""
    out: dict[str, PublicEventSpec] = {}
    for spec in all_specs(include_internal=include_internal):
        if spec.type not in out or spec.version > out[spec.type].version:
            out[spec.type] = spec
    return types.MappingProxyType(out)


def spec_for(event_type: str, version: int | None = None) -> PublicEventSpec:
    """Return the spec of ``event_type`` (latest version unless ``version`` is given); ``KeyError`` when unknown."""
    if version is not None:
        return _specs[event_type, version]
    found = [s for (t, _), s in _specs.items() if t == event_type]
    if not found:
        raise KeyError(event_type)
    return max(found, key=lambda s: s.version)


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        found.extend(_models_in(arg))
    return found


def _pii_fields(model: type[BaseModel], prefix: str = "", seen: set[type] | None = None) -> list[str]:
    seen = seen if seen is not None else set()
    if model in seen:
        return []
    seen.add(model)
    bad: list[str] = []
    for name, field in model.model_fields.items():
        names = {name, field.alias} - {None}
        if any(str(n).lower() in PII_FIELD_DENYLIST for n in names):
            bad.append(f"{prefix}{name}")
        for nested in _models_in(field.annotation):
            bad.extend(_pii_fields(nested, f"{prefix}{name}.", seen))
    return bad


def _known_scopes() -> set[str] | None:
    try:
        from app.core import oauth_scopes
    except ImportError:
        return None
    try:
        return {str(k) for k in oauth_scopes.scopes()}
    except Exception:
        return None


def validate_spec(spec: PublicEventSpec, known_scopes: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    label = f"{spec.type} v{spec.version}"
    if not TYPE_RE.match(spec.type):
        errors.append(f"{label}: type must match {TYPE_RE.pattern}")
    if spec.version < 1:
        errors.append(f"{label}: version must be >= 1")
    if spec.visibility not in ("public", "internal"):
        errors.append(f"{label}: visibility {spec.visibility!r}")
    if spec.data_model.model_config.get("extra") != "forbid":  # type: ignore[attr-defined]
        errors.append(f"{label}: data_model must set extra='forbid'")
    errors.extend(f"{label}: data field {f!r} is in PII_FIELD_DENYLIST" for f in _pii_fields(spec.data_model))
    if known_scopes is not None:
        errors.extend(f"{label}: unknown scope {s!r}" for s in spec.scopes if s not in known_scopes)
    return errors


def validate_catalogue() -> list[str]:
    """Post-discovery check: type grammar, unique (type, version), ``extra='forbid'``, no PII field name (nested too),
    scopes exist in ``core.oauth_scopes`` (skipped while that catalogue is empty or absent)."""
    known = _known_scopes()
    if not known:
        known = None
    errors: list[str] = []
    seen: set[tuple[str, int]] = set()
    for spec in all_specs(include_internal=True):
        key = (spec.type, spec.version)
        if key in seen:  # pragma: no cover - the registry already refuses duplicates
            errors.append(f"{spec.type} v{spec.version}: duplicate")
        seen.add(key)
        errors.extend(validate_spec(spec, known))
    return errors


__all__ = [
    "DYNAMIC_SUBJECT",
    "PII_FIELD_DENYLIST",
    "TYPE_RE",
    "DispatchKicker",
    "OutboxDraft",
    "OutboxRowFactory",
    "PublicEventSpec",
    "all_specs",
    "catalogue",
    "draft_for",
    "get_dispatch_kicker",
    "register_public_event",
    "set_dispatch_kicker",
    "set_outbox_row_factory",
    "spec_for",
    "unregister_public_event",
    "validate_catalogue",
    "validate_spec",
]

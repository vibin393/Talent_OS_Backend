"""Candidate change events for search / embeddings / matching (Epic 5).

A ``before_flush`` listener looks at every ORM write to a candidate or to the child rows that feed search and
matching, and emits (once per transaction and candidate) ``CandidateCreated``, ``CandidateUpdated``,
``CandidateSkillsChanged``, ``CandidateResumeParsed``, ``CandidateArchived``, ``AIConsentWithdrawn`` or
``AIConsentGranted`` through ``app.core.events.emit`` -- dispatched only after commit. Hooking the flush
(instead of each service function) covers every write path: API edits, resume apply, bulk import, merge,
privacy consent recompute and erasure. Core (non-ORM) bulk writes are not seen; the search reindex and the
matching sweep are the safety net for those.
"""

import uuid
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, SessionTransaction, UOWTransaction

from app.core.events import emit
from app.events.candidates import (
    AIConsentGranted,
    AIConsentWithdrawn,
    CandidateArchived,
    CandidateCreated,
    CandidateResumeParsed,
    CandidateSkillsChanged,
    CandidateUpdated,
)
from app.models.candidates import (
    Candidate,
    CandidateCertification,
    CandidateEmail,
    CandidateEmployment,
    CandidatePhone,
    CandidateResume,
    CandidateSkill,
    CandidateTag,
)

_SEEN_KEY = "epic5_candidate_change_events"
_IGNORED = {"updated_at", "created_at"}
_PROFILE_CHILDREN = (
    CandidateEmail,
    CandidatePhone,
    CandidateEmployment,
    CandidateCertification,
    CandidateTag,
)
_RESUME_FIELDS = {"candidate_id", "extracted_text", "parse_status", "parsed_data"}

_registered = False


def _changed(obj: Any) -> set[str]:
    state = inspect(obj)
    return {a.key for a in state.attrs if a.key not in _IGNORED and a.history.has_changes()}


def _old_new(obj: Any, key: str) -> tuple[Any, Any]:
    hist = inspect(obj).attrs[key].history
    old = hist.deleted[0] if hist.deleted else None
    new = hist.added[0] if hist.added else getattr(obj, key, None)
    return old, new


def _tenant(session: Session, obj: Any) -> uuid.UUID | None:
    raw = getattr(obj, "tenant_id", None) or session.info.get("tenant_id")
    return uuid.UUID(str(raw)) if raw is not None else None


def _emit(session: Session, cls: type, tenant_id: uuid.UUID | None, candidate_id: uuid.UUID | None) -> None:
    if tenant_id is None or candidate_id is None:
        return
    seen: set[tuple[str, uuid.UUID]] = session.info.setdefault(_SEEN_KEY, set())
    key = (cls.__name__, candidate_id)
    if key in seen:
        return
    seen.add(key)
    emit(session, cls(tenant_id=tenant_id, candidate_id=candidate_id))


def _candidate_changes(session: Session, obj: Candidate) -> None:
    tid = _tenant(session, obj)
    changed = _changed(obj)
    if not changed:
        return
    if "ai_consent_state" in changed:
        old, new = _old_new(obj, "ai_consent_state")
        if new == "withdrawn" and old != "withdrawn":
            _emit(session, AIConsentWithdrawn, tid, obj.id)
        elif old == "withdrawn" and new != "withdrawn":
            _emit(session, AIConsentGranted, tid, obj.id)
    if ("archived_at" in changed and obj.archived_at is not None) or (
        "merged_into_id" in changed and obj.merged_into_id is not None
    ):
        _emit(session, CandidateArchived, tid, obj.id)
    if changed - {"ai_consent_state"}:
        _emit(session, CandidateUpdated, tid, obj.id)


def _on_flush(session: Session, _ctx: UOWTransaction, _instances: object) -> None:
    for obj in list(session.new):
        if isinstance(obj, Candidate):
            _emit(session, CandidateCreated, _tenant(session, obj), obj.id)
        elif isinstance(obj, CandidateSkill):
            _emit(session, CandidateSkillsChanged, _tenant(session, obj), obj.candidate_id)
        elif isinstance(obj, _PROFILE_CHILDREN):
            _emit(session, CandidateUpdated, _tenant(session, obj), obj.candidate_id)
        elif isinstance(obj, CandidateResume) and obj.candidate_id is not None:
            _emit(session, CandidateResumeParsed, _tenant(session, obj), obj.candidate_id)
    for obj in list(session.dirty):
        if not session.is_modified(obj, include_collections=False):
            continue
        if isinstance(obj, Candidate):
            _candidate_changes(session, obj)
        elif isinstance(obj, CandidateSkill):
            _emit(session, CandidateSkillsChanged, _tenant(session, obj), obj.candidate_id)
        elif isinstance(obj, _PROFILE_CHILDREN):
            _emit(session, CandidateUpdated, _tenant(session, obj), obj.candidate_id)
        elif (
            isinstance(obj, CandidateResume)
            and obj.candidate_id is not None
            and _changed(obj) & _RESUME_FIELDS
        ):
            _emit(session, CandidateResumeParsed, _tenant(session, obj), obj.candidate_id)
    for obj in list(session.deleted):
        if isinstance(obj, CandidateSkill):
            _emit(session, CandidateSkillsChanged, _tenant(session, obj), obj.candidate_id)
        elif isinstance(obj, _PROFILE_CHILDREN):
            _emit(session, CandidateUpdated, _tenant(session, obj), obj.candidate_id)


def _clear(session: Session) -> None:
    session.info.pop(_SEEN_KEY, None)


def _clear_after_rollback(session: Session, previous: SessionTransaction) -> None:
    if previous.parent is None:
        session.info.pop(_SEEN_KEY, None)


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    event.listen(Session, "before_flush", _on_flush)
    event.listen(Session, "after_commit", _clear)
    event.listen(Session, "after_soft_rollback", _clear_after_rollback)
    _registered = True

# ruff: noqa: E501
"""Voluntary self-identification (Epic 49 A2/A3, FR-246, NFR-38).

* Store: ``demographic_self_ids`` (one sealed answer document per Candidate, key purpose ``demographic``). The
  RESTRICTIVE ``fairness_access`` policy hides every row from internal sessions unless ``access(db, mode)`` set
  ``app.fairness_access`` for the current transaction (``aggregate`` | ``erasure`` | ``apply_write``); Candidate
  sessions (org kind ``worker``) see their own row only.
* The ONLY writers are ``upsert_from_portal`` and ``upsert_from_apply`` (AST test). The only individual read is
  ``portal_get`` (the signed-in Candidate's own answers).
* Audit records ``self_id.updated`` / ``self_id.deleted`` without values; nothing here logs answers.
"""

import hashlib
import uuid
from typing import Any, Literal

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.encrypted import Sealed
from app.core.errors import Gone, NotFound
from app.models.fairness import DemographicSelfId, SelfIdApplyUse
from app.models.pipeline import Application
from app.models.portal import ApplySession, ApplySessionLookup
from app.schemas.fairness import NoticeOut, PublicSelfIdIn, SelfIdFormOut, SelfIdIn, SelfIdOut
from app.services import privacy
from app.services.fairness import categories, crypto
from app.services.fairness import settings as settings_mod
from app.services.fairness.common import CONSENT_PURPOSE, CONSENT_SOURCE, conflict, now, tenant_of

AccessMode = Literal["aggregate", "erasure", "apply_write"]
_MODES = ("aggregate", "erasure", "apply_write")


async def access(db: AsyncSession, mode: AccessMode) -> None:
    """``SET LOCAL app.fairness_access = mode`` for the current transaction (the aggregate task, erasure /
    withdrawal and the apply-form write only)."""
    if mode not in _MODES:
        msg = f"unknown fairness access mode {mode!r}"
        raise ValueError(msg)
    await db.execute(text("SELECT set_config('app.fairness_access', :m, true)"), {"m": mode})


async def end_access(db: AsyncSession) -> None:
    await db.execute(text("SELECT set_config('app.fairness_access', '', true)"))


# ---------------------------------------------------------------- notice / form
async def notice_out(db: AsyncSession) -> NoticeOut | None:
    notice = await privacy.consent.active_notice(db, tenant_of(db), CONSENT_PURPOSE)
    if notice is None:
        return None
    return NoticeOut(id=notice.id, version=notice.version, title=notice.title, text=notice.body_md)


async def _form(db: AsyncSession, enabled: bool) -> tuple[SelfIdFormOut, list[Any], Any]:
    cset = await categories.active_set(db)
    dims = await categories.dimensions(db, cset.id)
    if not enabled:
        return SelfIdFormOut(enabled=False, notice=None, dimensions=[]), dims, cset
    from app.schemas.fairness import DimensionOut

    return (
        SelfIdFormOut(
            enabled=True,
            notice=await notice_out(db),
            dimensions=[DimensionOut.model_validate(categories.dimension_out(d)) for d in dims if d.enabled],
        ),
        dims,
        cset,
    )


async def _require_notice(db: AsyncSession, notice_id: uuid.UUID) -> Any:
    notice = await privacy.consent.active_notice(db, tenant_of(db), CONSENT_PURPOSE)
    if notice is None or notice.id != notice_id:
        raise conflict(
            "notice_outdated", "The self-identification notice has changed; please review it again"
        )
    return notice


async def _upsert(
    db: AsyncSession,
    candidate_id: uuid.UUID,
    answers: dict[str, str],
    cset: Any,
    consent_id: uuid.UUID | None,
    source: str,
) -> None:
    tenant_id = tenant_of(db)
    sealed = crypto.seal_answers(tenant_id, answers)
    row = (
        await db.execute(select(DemographicSelfId).where(DemographicSelfId.candidate_id == candidate_id))
    ).scalar_one_or_none()
    if row is None:
        db.add(
            DemographicSelfId(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                set_id=cset.id,
                set_version=cset.version,
                answers=sealed,
                consent_id=consent_id,
                source=source,
            )
        )
    else:
        row.answers = sealed
        row.set_id = cset.id
        row.set_version = cset.version
        row.consent_id = consent_id
        row.source = source
        row.updated_at = now()
    await db.flush()
    audit.record("self_id.updated", "candidate", candidate_id, session=db, source=source)


# ---------------------------------------------------------------- Candidate Portal
async def _portal_enabled(db: AsyncSession) -> bool:
    s = await settings_mod.current(db)
    return bool(s.self_id_enabled and s.self_id_in_portal)


async def portal_get(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> SelfIdOut:  # noqa: ARG001
    """Return the signed-in Candidate's own answers (the only individual read path; ``db`` is the Candidate's
    worker-scoped session, so RLS returns their row only)."""
    enabled = await _portal_enabled(db)
    form, _dims, _cset = await _form(db, enabled)
    answers: dict[str, str] | None = None
    row = (
        await db.execute(select(DemographicSelfId).where(DemographicSelfId.candidate_id == candidate_id))
    ).scalar_one_or_none()
    if row is not None:
        opened = crypto.open_answers(row.tenant_id, row.answers)
        answers = {str(k): str(v) for k, v in opened.items()}
    if not enabled and answers is None:
        return SelfIdOut(enabled=False, notice=None, dimensions=[], answers=None)
    if not enabled:  # switched off later: the Candidate can still view / delete what is on file
        form, _dims, _cset = await _form(db, True)
        form = SelfIdFormOut(enabled=False, notice=form.notice, dimensions=form.dimensions)
    return SelfIdOut(enabled=form.enabled, notice=form.notice, dimensions=form.dimensions, answers=answers)


async def upsert_from_portal(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: SelfIdIn
) -> SelfIdOut:
    if not await _portal_enabled(db):
        raise NotFound("Self-identification is not offered")
    notice = await _require_notice(db, data.notice_id)
    _form_out, dims, cset = await _form(db, True)
    answers = categories.validate_answers(dims, data.answers)
    consent = await privacy.consent.record(
        db, candidate_id, CONSENT_PURPOSE, CONSENT_SOURCE, notice.version, ctx
    )
    await _upsert(db, candidate_id, answers, cset, consent.id, "portal")
    return await portal_get(ctx, db, candidate_id)


async def delete_from_portal(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> None:
    """Delete the answers and withdraw the consent in one transaction."""
    res = await db.execute(delete(DemographicSelfId).where(DemographicSelfId.candidate_id == candidate_id))
    deleted = int(getattr(res, "rowcount", 0) or 0)
    await privacy.consent.withdraw_consent(ctx, db, candidate_id, CONSENT_PURPOSE, by_kind="candidate")
    if deleted:
        audit.record("self_id.deleted", "candidate", candidate_id, session=db, by="candidate")


# ---------------------------------------------------------------- apply form (public)
async def apply_form(slug: str) -> SelfIdFormOut:
    from app.services.portal.careers import resolve_site

    site = await resolve_site(slug)
    async with tenant_session(site.tenant_id) as db:
        s = await settings_mod.current(db)
        form, _d, _c = await _form(db, bool(s.self_id_enabled and s.self_id_on_apply))
        return form


async def upsert_from_apply(slug: str, data: PublicSelfIdIn, ip: str | None = None) -> None:  # noqa: ARG001
    """Apply success step: the apply token must belong to a submitted, unexpired session of this tenant; one
    submission per apply session. Written under ``app.fairness_access = 'apply_write'``."""
    from app.services.portal.careers import resolve_site

    site = await resolve_site(slug)
    digest = hashlib.sha256(data.apply_token.encode("utf-8")).hexdigest()
    async with tenant_session(site.tenant_id) as db:
        audit.set_actor(db, "system", None)
        s = await settings_mod.current(db)
        if not (s.self_id_enabled and s.self_id_on_apply):
            raise NotFound("Self-identification is not offered")
        lookup = await db.get(ApplySessionLookup, digest)
        if lookup is None or lookup.tenant_id != site.tenant_id:
            raise NotFound("Apply session not found", code="apply_session_not_found")
        row = await db.get(ApplySession, lookup.session_id, with_for_update=True)
        if row is None or row.token_hash != digest or row.submitted_at is None or row.application_id is None:
            raise NotFound("Apply session not found", code="apply_session_not_found")
        if row.expires_at <= now():
            raise Gone("This application session has expired", code="apply_session_expired")
        if await db.get(SelfIdApplyUse, row.id) is not None:
            raise conflict("self_id_already_submitted", "Your answers were already received")
        candidate_id = (
            await db.execute(select(Application.candidate_id).where(Application.id == row.application_id))
        ).scalar_one_or_none()
        if candidate_id is None:
            raise NotFound("Apply session not found", code="apply_session_not_found")
        notice = await _require_notice(db, data.notice_id)
        _form_out, dims, cset = await _form(db, True)
        answers = categories.validate_answers(dims, data.answers)
        consent = await privacy.consent.record(
            db, candidate_id, CONSENT_PURPOSE, CONSENT_SOURCE, notice.version, None
        )
        db.add(SelfIdApplyUse(apply_session_id=row.id, tenant_id=site.tenant_id, candidate_id=candidate_id))
        await access(db, "apply_write")
        await _upsert(db, candidate_id, answers, cset, consent.id, "apply")
        await end_access(db)


# ---------------------------------------------------------------- erasure / withdrawal / aggregate
async def delete_for_candidate(db: AsyncSession, candidate_ids: list[uuid.UUID], *, reason: str) -> int:
    """Erasure / consent withdrawal: delete the subject's row(s) under ``app.fairness_access = 'erasure'``."""
    if not candidate_ids:
        return 0
    await access(db, "erasure")
    res = await db.execute(delete(DemographicSelfId).where(DemographicSelfId.candidate_id.in_(candidate_ids)))
    await end_access(db)
    n = int(getattr(res, "rowcount", 0) or 0)
    for cid in candidate_ids[:1] if n else []:
        audit.record("self_id.deleted", "candidate", cid, session=db, by=reason)
    return n


async def remaining(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> int:
    await access(db, "erasure")
    n = len(
        list(
            (
                await db.execute(
                    select(DemographicSelfId.id).where(DemographicSelfId.candidate_id.in_(candidate_ids))
                )
            ).scalars()
        )
    )
    await end_access(db)
    return n


async def sealed_for(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> dict[uuid.UUID, Sealed]:
    """Aggregate task only: the sealed answers of ``candidate_ids`` (decrypted in memory by the caller)."""
    out: dict[uuid.UUID, Sealed] = {}
    if not candidate_ids:
        return out
    await access(db, "aggregate")
    for start in range(0, len(candidate_ids), 5000):
        chunk = candidate_ids[start : start + 5000]
        rows = await db.execute(
            select(DemographicSelfId.candidate_id, DemographicSelfId.answers).where(
                DemographicSelfId.candidate_id.in_(chunk)
            )
        )
        for cid, sealed in rows.tuples():
            out[cid] = sealed
    await end_access(db)
    return out

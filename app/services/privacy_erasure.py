"""Privacy-domain erasure handlers (Epic 12, FR-68) and retention signals.

* ``privacy.consents`` (45): consent ``evidence`` -> NULL; the subject's secure links revoked, metadata cleared.
* ``privacy.records`` (46): requester name/email of the candidate's DSRs -> NULL (``human_id``, kind, dates and
  outcome stay as proof of compliance); reasons of released legal holds -> ``[erased]``.
* ``audit.shred`` (1000, always last): destroys the data keys of ``candidate:<id>`` (and merged records) and of
  their DSRs, so audited personal values -- including those written by the anonymising updates -- read
  ``[erased]``.

``NON_PERSONAL_TABLES`` in the coverage test lists ``erasure_runs`` / ``retention_run_items`` / ``retention_runs``
(ids, statuses and counts only).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.models.privacy import Consent, DsrRequest, LegalHold
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_activity_signal, register_erasure_handler

ERASED = "[erased]"


class ConsentsHandler:
    name = "privacy.consents"
    order = 45
    tables = ("consents", "secure_links")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        consents = list((await db.execute(select(Consent).where(Consent.candidate_id.in_(ids)))).scalars())
        for row in consents:
            row.evidence = None
        links = 0
        for cid in ids:
            for link in await secure_links.links_for_subject(db, "candidate", cid):
                link.meta = {}
                if link.revoked_at is None:
                    await secure_links.revoke(db, link.id)
                links += 1
        await db.flush()
        return {"consents": len(consents), "secure_links": links}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (await db.execute(select(Consent).where(Consent.candidate_id == ctx.candidate_id))).scalars()
        return {
            "consents": [
                {
                    "purpose": r.purpose,
                    "status": r.status,
                    "source": r.source,
                    "granted_at": r.granted_at,
                    "withdrawn_at": r.withdrawn_at,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings = [
            f"consent {cid} evidence remains"
            for cid in (
                await db.execute(select(Consent.id).where(Consent.candidate_id.in_(ids), Consent.evidence.is_not(None)))
            ).scalars()
        ]
        for cid in ids:
            for link in await secure_links.links_for_subject(db, "candidate", cid):
                await db.refresh(link)
                if link.revoked_at is None or link.meta:
                    findings.append(f"secure link {link.id} still usable")
        return findings


class PrivacyRecordsHandler:
    name = "privacy.records"
    order = 46
    tables = ("dsr_requests", "legal_holds")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        dsrs = list((await db.execute(select(DsrRequest).where(DsrRequest.candidate_id.in_(ids)))).scalars())
        for row in dsrs:
            row.requester_name = None
            row.requester_email = None
        holds = list(
            (
                await db.execute(
                    select(LegalHold).where(LegalHold.candidate_id.in_(ids), LegalHold.released_at.is_not(None))
                )
            ).scalars()
        )
        for hold in holds:
            hold.reason = ERASED
        await db.flush()
        return {"dsr_requests": len(dsrs), "legal_holds": len(holds)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (await db.execute(select(DsrRequest).where(DsrRequest.candidate_id == ctx.candidate_id))).scalars()
        return {
            "requests": [
                {"id": r.human_id, "kind": r.kind, "status": r.status, "received_at": r.received_at} for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left = await db.execute(
            select(DsrRequest.human_id).where(
                DsrRequest.candidate_id.in_(ids),
                (DsrRequest.requester_name.is_not(None)) | (DsrRequest.requester_email.is_not(None)),
            )
        )
        return [f"{human_id} requester details remain" for human_id in left.scalars()]


class AuditShredHandler:
    name = "audit.shred"
    order = 1000
    tables = ("audit_logs", "subject_keys")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        dsr_ids = list((await db.execute(select(DsrRequest.id).where(DsrRequest.candidate_id.in_(ids)))).scalars())
        for cid in ids:
            await audit.shred("candidate", cid, session=db)
        for dsr_id in dsr_ids:
            await audit.shred("dsr_request", dsr_id, session=db)
        return {"subjects_shredded": len(ids) + len(dsr_ids)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:  # ruff: ignore[unused-method-argument]
        return None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:  # ruff: ignore[unused-method-argument]
        return []  # runs after verification; shredding is idempotent and checked by tests


async def consent_signal(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """Retention: the last consent grant counts as activity."""
    if not candidate_ids:
        return {}
    rows = await db.execute(
        select(Consent.candidate_id, func.max(Consent.granted_at))
        .where(Consent.candidate_id.in_(candidate_ids), Consent.granted_at.is_not(None))
        .group_by(Consent.candidate_id)
    )
    return dict(rows.tuples().all())  # type: ignore[arg-type]


register_erasure_handler(ConsentsHandler())
register_erasure_handler(PrivacyRecordsHandler())
register_erasure_handler(AuditShredHandler())
register_activity_signal("privacy.consent_granted", consent_signal)

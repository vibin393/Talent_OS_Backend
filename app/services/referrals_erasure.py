# ruff: noqa: E501
"""Referral erasure handler ``referrals.records`` (47), Epic 42 A9 (FR-258, AD-34).

* **Referee as subject** (``referrals.candidate_id`` in the subject's ids, or the subject's email on a referral
  not yet linked to a Candidate): name, email, phone, relationship and note become ``[erased]`` / NULL and the
  resume file is tombstoned. Status and the reward stay: the reward belongs to the referrer and is a finance
  record.
* **Worker referrer as subject**: share links are deleted; referrals they made keep their shell (the referee is
  another person); rewards keep their amounts under the finance retention class -- ``referrer_candidate_id`` stays
  while retention applies (``finance_records``).
* ``export`` returns the referrals the subject made or received (referrer-safe fields only).
"""

from typing import Any

from sqlalchemy import ColumnElement, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.referrals import Referral, ReferralReward, ReferralShareLink
from app.schemas.referrals_referrer import status_label
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


async def _emails(db: AsyncSession, ids: list[Any]) -> list[str]:
    from app.models.candidates import CandidateEmail

    return [
        str(e)
        for e in (
            await db.execute(select(CandidateEmail.email).where(CandidateEmail.candidate_id.in_(ids)))
        ).scalars()
    ]


async def _received(db: AsyncSession, ids: list[Any]) -> list[Referral]:
    emails = await _emails(db, ids)
    cond: ColumnElement[bool] = Referral.candidate_id.in_(ids)
    if emails:
        cond = or_(cond, Referral.referee_email.in_(emails))
    return list((await db.execute(select(Referral).where(cond))).scalars())


class ReferralsHandler:
    name = "referrals.records"
    order = 47
    tables: tuple[str, ...] = ("referrals", "referral_share_links", "referral_rewards")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        from app.services import files_service

        ids = await subject_ids(db, ctx.candidate_id)
        counts = {"referrals": 0, "share_links": 0}
        for r in await _received(db, ids):
            if r.resume_file_id is not None:
                await files_service.purge_file(db, r.resume_file_id)
            r.referee_name = ERASED
            r.referee_email = None
            r.referee_phone = None
            r.relationship = None
            r.note = None
            counts["referrals"] += 1
        res = await db.execute(
            delete(ReferralShareLink).where(ReferralShareLink.referrer_candidate_id.in_(ids))
        )
        counts["share_links"] = int(getattr(res, "rowcount", 0) or 0)
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        made = list(
            (
                await db.execute(select(Referral).where(Referral.referrer_candidate_id == ctx.candidate_id))
            ).scalars()
        )
        received = await _received(db, [ctx.candidate_id])
        rewards = list(
            (
                await db.execute(
                    select(ReferralReward).where(ReferralReward.referrer_candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        if not made and not received and not rewards:
            return None
        return {
            "referrals_made": [
                {
                    "reference": r.human_id,
                    "referred_person": r.referee_name,
                    "status": status_label(r.status),
                    "on": r.created_at,
                }
                for r in made
            ],
            "referrals_received": [
                {"reference": r.human_id, "status": status_label(r.status), "on": r.created_at}
                for r in received
            ],
            "referral_rewards": [
                {"amount": str(w.amount), "currency": w.currency, "status": w.status, "paid_at": w.paid_at}
                for w in rewards
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        for r in await _received(db, ids):
            if (
                r.referee_email is not None
                or r.referee_phone is not None
                or (r.referee_name not in (None, ERASED))
            ):
                findings.append(f"referee contact remains on {r.human_id}")
        if (
            await db.execute(
                select(ReferralShareLink.id).where(ReferralShareLink.referrer_candidate_id.in_(ids))
            )
        ).first() is not None:
            findings.append("referral share links remain")
        return findings


register_erasure_handler(ReferralsHandler())

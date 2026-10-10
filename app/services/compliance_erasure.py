"""Compliance erasure handler ``compliance.credentials`` (59), Epic 21 A10 (AD-34, AD-35, NFR-26).

Claims ``credentials``, ``credential_alerts`` and ``placement_compliance_items``.

* Erase: ``number`` / ``number_bidx`` nulled (``number_last4`` kept only on retained rows), ``issuer`` nulled,
  ``rejection_note`` and ``waived_reason`` become ``[erased]``, the linked document bytes are deleted (the
  documents handler covers them too; deletion is idempotent). Types, dates and statuses carry no identity.
* Retain: rows in ``ctx.retained['credentials']`` (referenced by a retained signed agreement or financial
  record, class ``worker_profile``) are kept and reported.
* Export: type, issuer, dates, statuses, last 4 digits. Verify: no ``number`` on erased rows.

``credential_types``, ``compliance_requirements``, ``compliance_settings``, ``compliance_recompute_runs`` and
``compliance_evaluation_runs`` hold configuration / run bookkeeping only (no candidate column).
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compliance import Credential, CredentialAlert, CredentialType, PlacementComplianceItem
from app.services.candidates_erasure import subject_ids
from app.services.privacy.common import now
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"
RETENTION_CLASS = "worker_profile"


class ComplianceHandler:
    name = "compliance.credentials"
    order = 59
    tables = ("credentials", "credential_alerts", "placement_compliance_items")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        from app.services.compliance.credentials import purge_document

        ids = await subject_ids(db, ctx.candidate_id)
        retained = ctx.retained.get("credentials", set())
        counts = {"credentials": 0, "retained": 0, "files_deleted": 0, "alerts": 0, "items": 0}
        for c in (await db.execute(select(Credential).where(Credential.candidate_id.in_(ids)))).scalars():
            if c.id in retained:
                counts["retained"] += 1
                continue
            c.number = None
            c.number_bidx = None
            c.number_last4 = None
            c.issuer = None
            if c.rejection_note:
                c.rejection_note = ERASED
            counts["files_deleted"] += await purge_document(db, c.document_id)
            c.erased_at = c.erased_at or now()
            counts["credentials"] += 1
        result = await db.execute(delete(CredentialAlert).where(CredentialAlert.candidate_id.in_(ids)))
        counts["alerts"] = int(result.rowcount or 0)  # type: ignore[attr-defined]
        for it in (
            await db.execute(
                select(PlacementComplianceItem).where(PlacementComplianceItem.candidate_id.in_(ids))
            )
        ).scalars():
            if it.waived_reason:
                it.waived_reason = ERASED
            counts["items"] += 1
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        creds = list(
            (
                await db.execute(select(Credential).where(Credential.candidate_id == ctx.candidate_id))
            ).scalars()
        )
        if not creds:
            return None
        labels = {t.id: t.label for t in (await db.execute(select(CredentialType))).scalars()}
        return {
            "credentials": [
                {
                    "reference": c.human_id,
                    "type": labels.get(c.credential_type_id, c.type_key),
                    "issuer": c.issuer,
                    "issued_on": c.issued_on,
                    "expires_on": c.expires_on,
                    "verification_status": c.verification_status,
                    "expiry_status": c.expiry_status,
                    "number_last4": c.number_last4,
                    "version": c.version_no,
                }
                for c in creds
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        retained = ctx.retained.get("credentials", set())
        findings: list[str] = []
        for c in (await db.execute(select(Credential).where(Credential.candidate_id.in_(ids)))).scalars():
            if c.id in retained:
                continue
            if c.number is not None or c.number_bidx is not None or c.issuer is not None:
                findings.append(f"credential {c.human_id} identity data remains")
        return findings


register_erasure_handler(ComplianceHandler())

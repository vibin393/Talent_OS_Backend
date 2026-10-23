# ruff: file-ignore[line-too-long]
"""Chase facts of a Worker (Epic 62 A3): everything ``plan.due_chases`` needs, read from the P2 records (no writes).

* Items: mandatory checklist items of open Placements (``pre_joining`` / ``joining_confirmed`` / ``active``) and the
  Worker's current credentials / permits with an ``expires_on`` (not superseded, not rejected, not linked to a chased
  checklist item).
* Last contact of the person: P2 onboarding reminders (case / task ``last_reminded_at``), BGV document reminders
  (``last_doc_reminder_at``), P2 credential alerts and the agent's own chases. Item contact: credential alerts of that
  credential and the agent's chases of that item.
* Portal last seen: the newest candidate session (``sessions.last_seen_at``).
* Channels: portal always; email with an address on record; SMS / WhatsApp with a phone that is not opted out
  (WhatsApp also needs the P2 WhatsApp consent). Unavailable channels carry their reason.
"""

import dataclasses
import uuid
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chasing import ComplianceChase
from app.services import entity_refs
from app.services.chasing.plan import ChaseFacts, ItemFact

OPEN_PLACEMENT = ("pre_joining", "joining_confirmed", "active")
CHANNELS = ("portal", "email", "sms", "whatsapp")


@dataclasses.dataclass(frozen=True, slots=True)
class FactsBundle:
    facts: ChaseFacts
    placements: dict[uuid.UUID, object]
    allowed_channels: tuple[str, ...]
    unavailable: tuple[tuple[str, str], ...]
    coordinator_id: uuid.UUID | None
    labels: dict[uuid.UUID, str]


def _max(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


async def portal_last_seen(db: AsyncSession, candidate_id: uuid.UUID) -> datetime | None:
    from app.models.identity import Session

    return (
        await db.execute(
            select(func.max(Session.last_seen_at)).where(
                Session.principal_type == "candidate", Session.principal_id == candidate_id
            )
        )
    ).scalar_one_or_none()


async def last_contact(db: AsyncSession, candidate_id: uuid.UUID) -> datetime | None:
    from app.models.bgv import BgvCase
    from app.models.compliance import CredentialAlert
    from app.models.onboarding import OnboardingCase, OnboardingTask

    values = []
    for stmt in (
        select(func.max(OnboardingCase.last_reminded_at)).where(OnboardingCase.candidate_id == candidate_id),
        select(func.max(OnboardingTask.last_reminded_at)).where(OnboardingTask.candidate_id == candidate_id),
        select(func.max(BgvCase.last_doc_reminder_at)).where(BgvCase.candidate_id == candidate_id),
        select(func.max(CredentialAlert.sent_at)).where(CredentialAlert.candidate_id == candidate_id),
        select(func.max(ComplianceChase.sent_at)).where(ComplianceChase.candidate_id == candidate_id),
    ):
        values.append((await db.execute(stmt)).scalar_one_or_none())
    return _max(*values)


async def channels(
    db: AsyncSession, candidate_id: uuid.UUID
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    from app.services.messaging import opt_outs
    from app.services.messaging.sending import has_whatsapp_consent, resolve_phone

    allowed = ["portal"]
    unavailable: list[tuple[str, str]] = []
    who = await entity_refs.addressee(db, "candidate", candidate_id)
    if who is not None and who.address:
        allowed.append("email")
    else:
        unavailable.append(("email", "no_address"))
    phone = await resolve_phone(db, "candidate", candidate_id)
    for channel in ("sms", "whatsapp"):
        if not phone:
            unavailable.append((channel, "no_phone"))
        elif await opt_outs.is_opted_out(db, channel, phone):
            unavailable.append((channel, "opted_out"))
        elif channel == "whatsapp" and not await has_whatsapp_consent(db, "candidate", candidate_id):
            unavailable.append((channel, "no_consent"))
        else:
            allowed.append(channel)
    return tuple(allowed), tuple(unavailable)


async def coordinator_for(
    db: AsyncSession, candidate_id: uuid.UUID, placement_ids: list[uuid.UUID]
) -> uuid.UUID | None:
    """Return the Placement's onboarding owner -> ``onboarding_settings.default_hr_owner_id`` -> an ``hr_onboarding`` user."""
    from app.models.onboarding import OnboardingCase, OnboardingSettings

    candidates: list[uuid.UUID] = []
    if placement_ids:
        rows = (
            await db.execute(
                select(OnboardingCase.owner_user_id)
                .where(
                    OnboardingCase.placement_id.in_(placement_ids), OnboardingCase.owner_user_id.is_not(None)
                )
                .order_by(OnboardingCase.created_at.desc())
            )
        ).scalars()
        candidates.extend(r for r in rows if r is not None)
    settings_row = (await db.execute(select(OnboardingSettings).limit(1))).scalar_one_or_none()
    if settings_row is not None and settings_row.default_hr_owner_id is not None:
        candidates.append(settings_row.default_hr_owner_id)
    candidates.extend(sorted(await entity_refs.role_user_ids(db, "hr_onboarding")))
    del candidate_id
    if not candidates:
        return None
    active = await entity_refs.active_user_ids(db, candidates)
    return next((c for c in candidates if c in active), None)


async def gather(db: AsyncSession, candidate_id: uuid.UUID, today: date, now: datetime) -> FactsBundle:  # ruff: ignore[unused-function-argument]
    from app.models.compliance import (
        ComplianceSettings,
        Credential,
        CredentialAlert,
        CredentialType,
        PlacementComplianceItem,
    )
    from app.models.placements import Placement

    placements = {
        p.id: p
        for p in (
            await db.execute(
                select(Placement).where(
                    Placement.candidate_id == candidate_id, Placement.status.in_(OPEN_PLACEMENT)
                )
            )
        ).scalars()
    }
    items_rows = (
        list(
            (
                await db.execute(
                    select(PlacementComplianceItem).where(
                        PlacementComplianceItem.placement_id.in_(list(placements)),
                        PlacementComplianceItem.mandatory.is_(True),
                        PlacementComplianceItem.closed_at.is_(None),
                    )
                )
            ).scalars()
        )
        if placements
        else []
    )
    creds = {
        c.id: c
        for c in (
            await db.execute(
                select(Credential).where(
                    Credential.candidate_id == candidate_id,
                    Credential.superseded_at.is_(None),
                    Credential.erased_at.is_(None),
                    Credential.verification_status != "rejected",
                )
            )
        ).scalars()
    }
    linked_ids = {i.credential_id for i in items_rows if i.credential_id is not None}
    extra_ids = [cid for cid in linked_ids if cid not in creds]
    if extra_ids:
        for c in (await db.execute(select(Credential).where(Credential.id.in_(extra_ids)))).scalars():
            creds[c.id] = c
    types = {t.id: t for t in (await db.execute(select(CredentialType))).scalars()}
    settings_row = (await db.execute(select(ComplianceSettings).limit(1))).scalar_one_or_none()
    default_offsets = (
        tuple(settings_row.default_alert_offsets or ()) if settings_row is not None else (60, 30, 7)
    )
    alerts: dict[uuid.UUID, datetime] = {}
    if creds:
        for cred_id, sent in (
            await db.execute(
                select(CredentialAlert.credential_id, func.max(CredentialAlert.sent_at))
                .where(CredentialAlert.credential_id.in_(list(creds)))
                .group_by(CredentialAlert.credential_id)
            )
        ).all():
            alerts[cred_id] = sent
    chases = list(
        (
            await db.execute(select(ComplianceChase).where(ComplianceChase.candidate_id == candidate_id))
        ).scalars()
    )
    chased: dict[uuid.UUID, set[int]] = {}
    last_chase: dict[uuid.UUID, datetime] = {}
    for ch in chases:
        chased.setdefault(ch.item_ref, set()).add(int(ch.offset_days))
        last_chase[ch.item_ref] = max(last_chase.get(ch.item_ref, ch.sent_at), ch.sent_at)

    def offsets_of(cred: object | None) -> tuple[int, ...]:
        if cred is None:
            return ()
        t = types.get(getattr(cred, "credential_type_id", None))  # type: ignore[arg-type]
        return tuple(t.alert_offsets) if t is not None and t.alert_offsets else default_offsets

    facts: list[ItemFact] = []
    labels: dict[uuid.UUID, str] = {}
    chased_creds: set[uuid.UUID] = set()
    for it in items_rows:
        p = placements[it.placement_id]
        cred = creds.get(it.credential_id) if it.credential_id else None
        if it.status in ("missing", "expiring", "expired") and cred is not None:
            chased_creds.add(cred.id)
        facts.append(
            ItemFact(
                item_kind="checklist_item", item_ref=it.id, label=it.label, status=it.status, placement_id=p.id,
                start_date=p.planned_start_date if p.status != "active" else (p.actual_start_date or p.planned_start_date),
                expires_on=cred.expires_on if cred is not None else None, mandatory=it.mandatory,
                p2_alert_offsets=offsets_of(cred),
                last_item_contact=_max(last_chase.get(it.id), alerts.get(cred.id) if cred is not None else None),
                chased_offsets=frozenset(chased.get(it.id, set())),
                post_deadline_chases=sum(1 for o in chased.get(it.id, set()) if o < 0),
            )
        )  # fmt: skip
        labels[it.id] = it.label
    for cred in creds.values():
        if cred.candidate_id != candidate_id or cred.superseded_at is not None or cred.expires_on is None:
            continue
        if cred.id in chased_creds:
            continue
        t = types.get(cred.credential_type_id)
        kind = "permit" if t is not None and t.category == "visa_work_permit" else "credential"
        label = t.label if t is not None else cred.type_key
        facts.append(
            ItemFact(
                item_kind=kind, item_ref=cred.id, label=label, status="current", expires_on=cred.expires_on,  # type: ignore[arg-type]
                p2_alert_offsets=offsets_of(cred), last_item_contact=_max(last_chase.get(cred.id), alerts.get(cred.id)),
                chased_offsets=frozenset(chased.get(cred.id, set())),
                post_deadline_chases=sum(1 for o in chased.get(cred.id, set()) if o < 0),
            )
        )  # fmt: skip
        labels[cred.id] = label
    allowed, unavailable = await channels(db, candidate_id)
    starts = tuple(p.planned_start_date for p in placements.values() if p.status != "active")
    chase_facts = ChaseFacts(
        candidate_id=candidate_id, now=now, items=tuple(facts), last_contact=await last_contact(db, candidate_id),
        portal_last_seen=await portal_last_seen(db, candidate_id), start_dates=starts,
    )  # fmt: skip
    return FactsBundle(
        facts=chase_facts, placements=placements, allowed_channels=allowed, unavailable=unavailable,  # type: ignore[arg-type]
        coordinator_id=await coordinator_for(db, candidate_id, list(placements)), labels=labels,
    )  # fmt: skip


__all__ = [
    "CHANNELS",
    "OPEN_PLACEMENT",
    "FactsBundle",
    "channels",
    "coordinator_for",
    "gather",
    "last_contact",
    "portal_last_seen",
]

# ruff: file-ignore[line-too-long, unused-function-argument]
"""Provider-delivered assessments (Epic 40 A10, FR-207, AD-49, NFR-43). Executed coding tests run ONLY at a provider.

* **Invite** -- ``queue_invite`` stores a ``connector_jobs`` row keyed ``assessment:invite:assessment_invitation:<id>:<n>``
  with the frozen minimal ``ProviderInviteDTO`` payload (tenant, invitation, external test, candidate email, display
  name, deadline, callback) and, after commit, the task ``assessments.provider_invite`` (queue ``connectors``) runs it
  through ``connectors.jobs.execute`` (bounded retries), stores ``provider_ref`` and emails the provider's take URL.
  The Manual adapter raises ``ManualActionRequired``: the job succeeds as "manual" and a User records the result.
* **Webhook** -- ``register_webhook_handler("assessment", on_webhook)``: the payload's ``external_ref`` ->
  ``fetch_result`` -> ``import_result``: provider skill names map through ``assessment_skill_map`` to ``SkillKey``s
  and to proficiency with the same pure map as built-in results; unmapped skills are kept for display and emit no
  evidence. The invitation moves to ``reviewed`` (system) and ``AssessmentScored(source="provider")`` is emitted.
* **Manual result** -- ``manual_result`` (``recorded_by_user``) walks ``invited|started|completed -> reviewed``.
* Pass / Fail stays a human (star) decision.
"""

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import require_human
from app.core.context import RequestContext
from app.core.events import emit
from app.core.skill_refs import SkillKey
from app.models.assessments import (
    Assessment,
    AssessmentInvitation,
    AssessmentProviderTest,
    AssessmentSkillMap,
)
from app.schemas.assessments import InvitationOut, ManualResultIn, ProviderTestOut, SkillMapIn, SkillMapOut
from app.services.assessments import invitations, lifecycle, results, scoring
from app.services.assessments.common import actor_of, conflict, invalid, not_found, now
from app.services.assessments.events import AssessmentProviderInviteQueued
from app.services.connectors.assessment.port import ProviderInviteDTO, ProviderResultDTO

CATEGORY = "assessment"


def job_key(inv: AssessmentInvitation) -> tuple[str, int]:
    version = inv.extended_count + 1
    return f"{CATEGORY}:invite:assessment_invitation:{inv.id}:{version}", version


def invite_payload(dto: ProviderInviteDTO) -> dict[str, Any]:
    """Return the whole outbound payload (minimal data, NFR-43): exactly the frozen DTO fields."""
    return {
        "tenant_id": str(dto.tenant_id),
        "invitation_id": str(dto.invitation_id),
        "external_test_id": dto.external_test_id,
        "candidate_email": dto.candidate_email,
        "candidate_name": dto.candidate_name,
        "deadline_at": dto.deadline_at.isoformat() if dto.deadline_at else None,
        "callback_url": dto.callback_url,
    }


async def queue_invite(
    ctx: RequestContext, db: AsyncSession, inv: AssessmentInvitation, assessment: Assessment
) -> uuid.UUID:
    from app.models.connectors import ConnectorJob

    email = await invitations.candidate_email(db, inv.candidate_id) or ""  # type: ignore[arg-type]
    name = (await invitations.candidate_names(db, [inv.candidate_id])).get(inv.candidate_id, "")  # type: ignore[list-item,arg-type]
    dto = ProviderInviteDTO(
        tenant_id=inv.tenant_id,
        invitation_id=inv.id,
        external_test_id=assessment.provider_test_ref or "",
        candidate_email=email,
        candidate_name=name,
        deadline_at=inv.deadline_at,
        callback_url=f"/api/v1/hooks/{CATEGORY}/{assessment.provider_config_id}"
        if assessment.provider_config_id
        else None,
    )
    key, version = job_key(inv)
    await db.execute(
        pg_insert(ConnectorJob)
        .values(
            id=uuid.uuid4(), tenant_id=inv.tenant_id, category=CATEGORY, op="invite", subject_kind="assessment_invitation",
            subject_id=inv.id, version=version, idempotency_key=key, config_id=assessment.provider_config_id, status="queued",
            attempts=0, max_attempts=5, payload=invite_payload(dto),
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
    )  # fmt: skip
    job_id = (
        await db.execute(select(ConnectorJob.id).where(ConnectorJob.idempotency_key == key))
    ).scalar_one()
    emit(db, AssessmentProviderInviteQueued(tenant_id=inv.tenant_id, invitation_id=inv.id, job_id=job_id))
    return job_id


async def run_invite(ctx: RequestContext, db: AsyncSession, job_id: uuid.UUID) -> int | None:
    """Task body (queue ``connectors``): one attempt; returns the retry delay (seconds) or ``None``."""
    from app.models.connectors import ConnectorJob
    from app.services.connectors import jobs

    status, retry_in = await jobs.execute(ctx, db, job_id)
    job = await db.get(ConnectorJob, job_id)
    if status != "succeeded" or job is None:
        return retry_in
    result = dict(job.result or {})
    inv = await db.get(AssessmentInvitation, job.subject_id, with_for_update=True)
    if inv is None or result.get("manual"):
        return None
    ref = result.get("external_ref")
    if isinstance(ref, str) and ref and inv.provider_ref != ref:
        inv.provider_ref = ref[:200]
        take_url = result.get("take_url")
        email = await invitations.candidate_email(db, inv.candidate_id) if inv.candidate_id else None
        if isinstance(take_url, str) and take_url and email:
            name = (await db.get(Assessment, inv.assessment_id)).name  # type: ignore[union-attr]
            await invitations._email(
                inv, email, f"Assessment: {name}",
                f"You are invited to take an online assessment.\n\nAssessment: {name}\nComplete it by: "
                f"{inv.deadline_at.strftime('%d %b %Y %H:%M UTC')}\n\nStart here: {take_url}",
                "assessment_invited",
            )  # fmt: skip
        await db.flush()
    return None


async def _find_by_ref(db: AsyncSession, external_ref: str) -> AssessmentInvitation | None:
    from app.models.connectors import ConnectorJob

    inv = (
        await db.execute(
            select(AssessmentInvitation)
            .where(AssessmentInvitation.provider_ref == external_ref)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if inv is not None:
        return inv
    job = (
        (
            await db.execute(
                select(ConnectorJob).where(
                    ConnectorJob.category == CATEGORY,
                    ConnectorJob.op == "invite",
                    ConnectorJob.result["external_ref"].astext == external_ref,
                )
            )
        )
        .scalars()
        .first()
    )
    if job is None:
        return None
    inv = await db.get(AssessmentInvitation, job.subject_id, with_for_update=True)
    if inv is not None and not inv.provider_ref:
        inv.provider_ref = external_ref[:200]
    return inv


async def _skill_map(db: AsyncSession, config_id: uuid.UUID | None) -> dict[str, str]:
    if config_id is None:
        return {}
    rows = await db.execute(
        select(AssessmentSkillMap.provider_skill, AssessmentSkillMap.skill_key).where(
            AssessmentSkillMap.config_id == config_id
        )
    )
    return {name.casefold(): key for name, key in rows.tuples()}


async def import_result(
    db: AsyncSession, inv: AssessmentInvitation, result: ProviderResultDTO, config_id: uuid.UUID | None
) -> bool:
    """Apply a completed provider result (idempotent: an already reviewed / decided invitation is left alone)."""
    if result.status != "completed" or result.score is None:
        return False
    if inv.status not in ("invited", "started", "completed"):
        return False
    mapping = await _skill_map(db, config_id)
    skill_scores: dict[str, Any] = {}
    evidence: list[tuple[str, int]] = []
    for name, value in sorted(result.skill_scores.items()):
        pct = min(max(Decimal(str(value)), Decimal(0)), Decimal(100)).quantize(Decimal("0.01"))
        key = mapping.get(name.casefold())
        if key:
            prof = scoring.proficiency_for(pct)
            skill_scores[key] = {"pct": str(pct), "proficiency": prof, "label": name}
            evidence.append((key, prof))
        else:
            skill_scores[f"provider:{name}"[:80]] = {
                "pct": str(pct),
                "proficiency": None,
                "label": name,
            }  # display only
    score = min(max(Decimal(str(result.score)), Decimal(0)), Decimal(100)).quantize(Decimal("0.01"))
    inv.score = score
    inv.points = score
    inv.max_points = Decimal(100)
    inv.section_scores = {
        k: {
            "title": k,
            "pct": str(Decimal(str(v)).quantize(Decimal("0.01"))),
            "points": None,
            "max_points": None,
        }
        for k, v in result.section_scores.items()
    }
    inv.skill_scores = skill_scores
    inv.report_url = (result.report_url or None) and result.report_url[:1000]
    if inv.status == "invited":
        await lifecycle.move(db, inv, "started", "system", provider=True)
    if inv.status == "started":
        await lifecycle.move(db, inv, "completed", "system")
    await lifecycle.move(db, inv, "reviewed", "system")
    await results.finalize(db, inv, source="provider", evidence=tuple(sorted(set(evidence))))
    return True


async def on_webhook(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any], config: Any) -> None:
    from app.services.connectors import registry

    ref = str(payload.get("external_ref") or "")
    if not ref:
        return
    inv = await _find_by_ref(db, ref)
    if inv is None:
        logger.info("assessment webhook: no invitation for the provider reference")
        return
    adapter: Any = registry.build(CATEGORY, config)
    result = await adapter.fetch_result(ref)
    if result is not None:
        await import_result(db, inv, result, getattr(config, "id", None))
    del ctx


# ---------------------------------------------------------------- manual result (Manual adapter)
async def manual_result(
    ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID, data: ManualResultIn
) -> InvitationOut:
    from app.services import files_service

    actor = actor_of(ctx)
    require_human(actor)
    if actor.kind != "user":
        raise conflict("transition_not_allowed", "Only a user records provider results")
    inv = await invitations.load(db, invitation_id, for_update=True)
    if inv.status not in ("invited", "started", "completed"):
        raise conflict("transition_not_allowed", "This invitation already has a result", status=inv.status)
    if data.report_file_id is not None and await files_service.get_file(db, data.report_file_id) is None:
        raise invalid("unknown_file", "The report file was not found", "report_file_id")
    evidence: list[tuple[str, int]] = []
    skill_scores: dict[str, Any] = {}
    for s in data.skill_scores:
        try:
            key = SkillKey.parse(s.skill_key).as_str()
        except ValueError as exc:
            raise invalid(
                "invalid_skill_key", f"Unknown skill key format: {s.skill_key}", "skill_scores"
            ) from exc
        skill_scores[key] = {"pct": None, "proficiency": s.proficiency, "label": None}
        evidence.append((key, s.proficiency))
    inv.score = data.score_pct.quantize(Decimal("0.01"))
    inv.points = inv.score
    inv.max_points = Decimal(100)
    inv.section_scores = {
        "recorded": {
            "title": "Recorded result",
            "pct": str(inv.score),
            "points": None,
            "max_points": None,
            "note": data.result_note,
        }
    }
    inv.skill_scores = skill_scores
    inv.report_file_id = data.report_file_id
    for target in ("started", "completed", "reviewed"):
        if inv.status != target and (inv.status, target) in lifecycle.TRANSITIONS:
            await lifecycle.move(db, inv, target, "recorded_by_user", actor_id=actor.id)
    await results.finalize(db, inv, source="provider", evidence=tuple(sorted(set(evidence))))
    return await invitations.detail(ctx, db, inv.id)


# ---------------------------------------------------------------- catalogue / skill map
async def _config(db: AsyncSession, config_id: uuid.UUID) -> Any:
    from app.models.connectors import ProviderConnectorConfig

    row = await db.get(ProviderConnectorConfig, config_id)
    if row is None or row.category != CATEGORY:
        raise not_found("Connector")
    return row


async def sync_catalogue(
    ctx: RequestContext, db: AsyncSession, config_id: uuid.UUID
) -> list[ProviderTestOut]:
    from app.services.connectors import registry

    row = await _config(db, config_id)
    adapter: Any = registry.build(CATEGORY, await registry.load_config(db, row))
    tests = await adapter.list_tests()
    t = now()
    for test in tests:
        await db.execute(
            pg_insert(AssessmentProviderTest)
            .values(
                id=uuid.uuid4(), tenant_id=row.tenant_id, config_id=row.id, external_ref=test.external_id, name=test.name[:300],
                duration_minutes=test.duration_minutes, skills=list(test.skill_tags), synced_at=t,
            )
            .on_conflict_do_update(
                index_elements=["config_id", "external_ref"],
                set_={"name": test.name[:300], "duration_minutes": test.duration_minutes, "skills": list(test.skill_tags), "synced_at": t},
            )
        )  # fmt: skip
    await db.flush()
    rows = (
        await db.execute(
            select(AssessmentProviderTest)
            .where(AssessmentProviderTest.config_id == row.id)
            .order_by(AssessmentProviderTest.name)
        )
    ).scalars()
    return [ProviderTestOut.model_validate(r) for r in rows]


async def get_skill_map(ctx: RequestContext, db: AsyncSession, config_id: uuid.UUID) -> SkillMapOut:
    await _config(db, config_id)
    rows = (
        await db.execute(
            select(AssessmentSkillMap)
            .where(AssessmentSkillMap.config_id == config_id)
            .order_by(AssessmentSkillMap.provider_skill)
        )
    ).scalars()
    return SkillMapOut(
        config_id=config_id,
        rows=[{"provider_skill": r.provider_skill, "skill_key": r.skill_key} for r in rows],
    )


async def put_skill_map(ctx: RequestContext, db: AsyncSession, data: SkillMapIn) -> SkillMapOut:
    row = await _config(db, data.config_id)
    existing = {
        r.provider_skill.casefold(): r
        for r in (
            await db.execute(select(AssessmentSkillMap).where(AssessmentSkillMap.config_id == row.id))
        ).scalars()
    }
    wanted: dict[str, str] = {}
    for r in data.rows:
        try:
            wanted[r.provider_skill.strip()] = SkillKey.parse(r.skill_key).as_str()
        except ValueError as exc:
            raise invalid("invalid_skill_key", f"Unknown skill key format: {r.skill_key}", "rows") from exc
    for name, key in wanted.items():
        cur = existing.pop(name.casefold(), None)
        if cur is None:
            db.add(
                AssessmentSkillMap(
                    id=uuid.uuid4(),
                    tenant_id=row.tenant_id,
                    config_id=row.id,
                    provider_skill=name,
                    skill_key=key,
                )
            )
        else:
            cur.skill_key = key
    for stale in existing.values():
        await db.delete(stale)
    await db.flush()
    return await get_skill_map(ctx, db, row.id)


__all__ = [
    "get_skill_map",
    "import_result",
    "invite_payload",
    "manual_result",
    "on_webhook",
    "put_skill_map",
    "queue_invite",
    "run_invite",
    "sync_catalogue",
]

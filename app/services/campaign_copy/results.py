# ruff: file-ignore[line-too-long]
"""Run outcome handling (Epic 78 A3): a finished ``campaign_copy`` run's result (validated ``CampaignCopyResult``)
becomes one draft per segment x variant (``generated`` -> checks -> ``checked``), the request ``generated`` with the
run's tokens / cost, ``CampaignCopyGenerated`` and the ``campaign_copy_ready`` notification. A failed / stopped run
(or an invalid result) -> request ``failed`` + ``campaign_copy_failed`` with the fallback message."""

import uuid
from typing import Any

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.events.campaign_copy import CampaignCopyGenerated
from app.models.campaign_copy import CampaignCopyDraft, CampaignCopyRequest
from app.schemas.campaign_copy import CampaignCopyResult
from app.services.campaign_copy import checks
from app.services.campaign_copy import context as facts
from app.services.campaign_copy.common import FALLBACK_MESSAGE, draft_hash
from app.services.campaign_copy.drafts import content_of


async def _notify(db: AsyncSession, req: CampaignCopyRequest, type_key: str, title: str) -> None:
    from app.services import identity_bridge
    from app.services import notifications_service as ns

    recipients: set[uuid.UUID] = {req.requested_by}
    if type_key == "campaign_copy_ready":
        recipients |= {uuid.UUID(str(u["id"])) for u in await identity_bridge.users_with_permission(db, "campaign:copy_approve")}
    target = f"/marketing/campaigns/{req.campaign_id}/copy/{req.id}" if req.campaign_id else f"/marketing/sequences/{req.sequence_id}"
    content = ns.Content(title=title[:300], link_path=target)
    try:
        await ns.deliver(db, type_key, recipients, content, event_id=f"{type_key}:{req.id}", payload={"request_id": str(req.id)})
    except Exception:
        logger.warning("campaign copy notification {} not delivered for request {}", type_key, req.id)


def _clip(value: Any, n: int) -> str | None:
    return str(value)[:n] if value is not None else None


async def _fail(db: AsyncSession, req: CampaignCopyRequest, code: str) -> None:
    req.status, req.error_code = "failed", code[:40]
    await db.flush()
    await _notify(db, req, "campaign_copy_failed", FALLBACK_MESSAGE)


async def finalize_from_run(db: AsyncSession, run_id: uuid.UUID, run_status: str, reason: str | None) -> str:
    from app.models.agent_runtime import AgentRun

    req = (
        await db.execute(select(CampaignCopyRequest).where(CampaignCopyRequest.agent_run_id == run_id).with_for_update())
    ).scalar_one_or_none()
    if req is None or req.status != "generating":
        return "skipped"
    run = await db.get(AgentRun, run_id)
    if run is not None:
        req.input_tokens, req.output_tokens, req.cost_micros = int(run.input_tokens or 0), int(run.output_tokens or 0), int(run.cost_micros or 0)
    if run_status != "completed" or run is None or not run.result:
        await _fail(db, req, reason or run_status or "failed")
        return "failed"
    try:
        result = CampaignCopyResult.model_validate(run.result)
    except ValidationError:
        await _fail(db, req, "invalid_result")
        return "failed"
    specs = {s["key"]: s for s in req.segments or []}
    ctx_facts = await facts.check_context(db, req)
    count = 0
    for seg in result.segments:
        spec = specs.get(seg.key)
        if spec is None:
            continue  # never trust a segment the request did not ask for
        for n, v in enumerate(seg.variants[: req.variants_per_segment], start=1):
            d = CampaignCopyDraft(
                id=uuid.uuid4(), tenant_id=req.tenant_id, request_id=req.id, campaign_id=req.campaign_id,
                sequence_id=req.sequence_id, sequence_step_id=req.sequence_step_id, step_position=req.step_position,
                segment_key=seg.key, segment_label=str(spec.get("label") or seg.key)[:120], variant_no=n, channel=req.channel,
                subject=_clip(v.subject, 200) if req.channel == "email" else None,
                preheader=_clip(v.preheader, 200) if req.channel == "email" else None,
                body_html=v.body_html if req.channel == "email" else None,
                template_values={str(k): str(x) for k, x in (v.template_values or {}).items()} if req.channel != "email" else None,
                job_link_blocks=[b.model_dump(mode="json") for b in v.job_link_blocks], status="generated", findings=[],
                content_sha256="0" * 64, ai_generated=True,
            )  # fmt: skip
            d.content_sha256 = draft_hash(d)
            d.findings = checks.run(content_of(d), ctx_facts)
            d.status = "checked"
            db.add(d)
            count += 1
    if count == 0:
        await _fail(db, req, "empty_result")
        return "failed"
    req.status = "generated"
    await db.flush()
    emit(db, CampaignCopyGenerated(
        tenant_id=req.tenant_id, request_id=req.id, campaign_id=req.campaign_id, sequence_step_id=req.sequence_step_id, drafts=count,
    ))  # fmt: skip
    await _notify(db, req, "campaign_copy_ready", f"AI campaign copy is ready for approval ({count} drafts)")
    return "generated"


__all__ = ["finalize_from_run"]

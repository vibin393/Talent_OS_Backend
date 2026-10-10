# ruff: noqa: E501, D400, D401, D415
"""Board applicant ingest (Epic 70, story 70.3; spec A4). Naukri responses (poll) and Indeed Apply (webhook) both land
in ``ingest_one``:

1. idempotent on ``(board_key, board_application_id)`` (``board_applicant_ingests``; the row never holds personal
   data);
2. the Board Posting from the connector link of the board reference (unknown -> ``failed``);
3. suppression: hashed email / phone of erased, withdrawn or Do-Not-Contact people, and an existing Candidate who is
   erased or withdrew ``data_processing`` -> ``suppressed``; nothing is created;
4. resume: sniffed + malware-scanned through ``files_service`` (rejected -> no file kept, Task); text extracted, no AI
   parse (board applicants have not given AI-processing consent);
5. candidate: existing (exact email) -> linked + new resume version (parsed data never applied); else a new
   ``job_board`` candidate (``source_detail`` = board key); duplicate review items as P1 (never merged);
6. application ``source=job_board``, ``source_ref=<board_posting_id>``; board answers stored as untrusted answers;
7. the ``job_board_application`` notice (consent evidence ``source=job_board``) and the notice email after commit.

Applicant text, answers and resumes are untrusted data (P4 NFR-45): stored as data, never interpreted.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.events import emit
from app.events.job_distribution import BoardApplicantIngested
from app.models.connectors import ConnectorInboundEvent, ProviderConnectorConfig
from app.models.demands import Demand
from app.models.job_distribution import BoardApplicantIngest, BoardPosting
from app.models.portal import ApplicationAnswer
from app.models.privacy import Consent
from app.services import integrations_service
from app.services.connectors.job_board.port import BoardApplicantDTO
from app.services.job_distribution import suppression
from app.services.job_distribution.common import board_label, now, tenant_of

NOTICE_PURPOSE = "job_board_application"


async def _withdrawn(db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    rows = list((await db.execute(select(Consent.status).where(Consent.candidate_id == candidate_id, Consent.purpose == "data_processing"))).scalars())
    return bool(rows) and "granted" not in rows


async def _resume_task(db: AsyncSession, bp: BoardPosting, title: str) -> None:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    demand = await db.get(Demand, bp.demand_id)
    owner = bp.published_by or (demand.owner_id if demand else None)
    if owner is None:
        return
    data = TaskCreate.model_construct(
        title=f"Resume from {board_label(bp.board_key)} was rejected for {title}"[:200],
        notes="The resume failed the file checks (type or malware scan) and was not stored. Ask the applicant for another copy.",
        owner_id=owner, entity_type="board_posting", entity_id=bp.id, due_at=None, due_all_day=False, priority="normal", reminder_at=None,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            await tasks_service.create_system_task(RequestContext(tenant_id=bp.tenant_id), db, data, source="agent")
    except Exception:  # pragma: no cover
        logger.exception("Resume-rejected Task for board posting {} not created", bp.id)


async def _store_resume(ctx: RequestContext, db: AsyncSession, a: BoardApplicantDTO) -> tuple[str, Any]:
    from app.services import files_service
    from app.services.candidates import portal_port as candidates

    if not a.resume:
        return "missing", None
    try:
        async with db.begin_nested():
            file = await files_service.store_upload(ctx, db, a.resume, a.resume_filename or "resume", files_service.RESUME_KINDS, None, None)
    except ValidationFailed:
        return "rejected", None
    extraction = candidates.extract(a.resume)
    return "stored", (file, extraction)


async def ingest_one(
    ctx: RequestContext,
    db: AsyncSession,
    installation_id: uuid.UUID,
    board_key: str,
    applicant: BoardApplicantDTO,
    *,
    inbound_event_id: uuid.UUID | None = None,
) -> str:
    """Returns ``created | linked | suppressed | failed | duplicate``."""
    tid = tenant_of(db)
    if not applicant.board_application_id:
        return "failed"
    row_id = (
        await db.execute(
            pg_insert(BoardApplicantIngest)
            .values(id=uuid.uuid4(), tenant_id=tid, installation_id=installation_id, board_key=board_key, board_application_id=applicant.board_application_id[:200], inbound_event_id=inbound_event_id, status="received")
            .on_conflict_do_nothing(constraint="uq_board_applicant_ingests_app")
            .returning(BoardApplicantIngest.id)
        )
    ).scalar_one_or_none()
    if row_id is None:
        return "duplicate"
    row = await db.get(BoardApplicantIngest, row_id)
    assert row is not None
    try:
        async with db.begin_nested():
            outcome = await _process(ctx, db, row, installation_id, board_key, applicant)
    except Exception as exc:
        logger.warning("Board applicant ingest {} failed: {}", row.id, type(exc).__name__)
        row.status, row.error_code = "failed", (getattr(exc, "code", None) or type(exc).__name__)[:60]
        outcome = "failed"
    row.processed_at = now()
    await db.flush()
    return outcome


async def _process(ctx: RequestContext, db: AsyncSession, row: BoardApplicantIngest, installation_id: uuid.UUID, board_key: str, a: BoardApplicantDTO) -> str:
    from app.services import privacy
    from app.services.candidates import portal_port as candidates
    from app.services.pipeline import portal_port as pipeline

    link = await integrations_service.find_link(db, installation_id, "board_posting", external_id=a.board_ref) if a.board_ref else None
    bp = await db.get(BoardPosting, link.local_id) if link is not None else None
    if bp is None:
        row.status, row.error_code = "failed", "unknown_board_posting"
        return "failed"
    row.board_posting_id = bp.id
    demand = await db.get(Demand, bp.demand_id)
    assert demand is not None
    # 3. suppression
    reason = await suppression.match(db, a.email, a.phone_e164)
    existing = await candidates.link_by_exact_email(ctx, db, a.email) if a.email else None
    if reason is None and existing is not None:
        st = await candidates.status(db, existing)
        if st is not None and st["erased"]:
            reason = "erased"
        elif await _withdrawn(db, existing):
            reason = "consent_withdrawn"
    if reason is not None:
        row.status, row.suppression_reason = "suppressed", reason
        audit.record("job_board.applicant_suppressed", "board_posting", bp.id, session=db, board_key=board_key, board_application_id=a.board_application_id)
        return "suppressed"
    # 4. resume
    resume_status, stored = await _store_resume(ctx, db, a)
    row.resume_status = resume_status
    if resume_status == "rejected":
        await _resume_task(db, bp, demand.title)
    # 5. candidate
    created = existing is None
    if existing is None:
        candidate_id = await candidates.create_from_application(
            ctx, db, owner_id=demand.owner_id, full_name=a.full_name[:200], email=candidates.clean_email(a.email) if a.email else None,
            phone=candidates.clean_phone(a.phone_e164) if a.phone_e164 else None, location=a.location, links=[],
            prefill={"current_title": a.current_title} if a.current_title else None, source="job_board", source_detail=board_key,
        )  # fmt: skip
    else:
        candidate_id = existing
    if stored is not None:
        file, extraction = stored
        await candidates.add_resume(
            ctx, db, candidate_id, file_id=file.id, text=extraction.text, text_status=extraction.status, parse_status="skipped_consent",
            parsed_data=None, ai_action_id=None, note=f"Resume received from {board_label(board_key)}",
        )  # fmt: skip
    # 6. application
    application_id = await pipeline.existing_application(db, demand.id, candidate_id)
    if application_id is None:
        application_id = await pipeline.create_candidate_application(ctx, db, demand.id, candidate_id, source="job_board", source_ref=str(bp.id))
        if a.answers:
            questions = [{"id": f"board:{i + 1}", "label": (q or f"Question {i + 1}")[:300], "type": "text"} for i, (q, _) in enumerate(a.answers)]
            db.add(ApplicationAnswer(id=uuid.uuid4(), tenant_id=row.tenant_id, application_id=application_id, questions=questions, answers={f"board:{i + 1}": ans[:2000] for i, (_, ans) in enumerate(a.answers)}, submitted_at=a.applied_at or now()))
    # 7. notice (consent evidence) + email after commit
    await privacy.consent.record(db, candidate_id, NOTICE_PURPOSE, "job_board", None, ctx, evidence={"basis": "notice", "board": board_key, "board_posting_id": str(bp.id)})
    row.status = "created" if created else "linked"
    row.candidate_id, row.application_id, row.created_candidate = candidate_id, application_id, created
    await db.flush()
    emit(db, BoardApplicantIngested(tenant_id=row.tenant_id, board_posting_id=bp.id, application_id=application_id, candidate_id=candidate_id, created_candidate=created, board_key=board_key, notify_email=bool(a.email)))
    return row.status


# ================================================================ webhook (Indeed Apply)
async def on_webhook(ctx: RequestContext, db: AsyncSession, payload: dict[str, Any], config: Any) -> None:
    """``connectors.process_inbound`` handler of the ``job_board`` category: the payload is the applicant push
    (authoritative). The stored resume bytes are replaced by ``<stored:{file_id}>`` afterwards (minimisation)."""
    from app.services.connectors.job_board.indeed import applicant_from_payload

    row = await db.get(ProviderConnectorConfig, config.id)
    if row is None or row.installation_id is None or not payload.get("id"):
        return
    applicant = applicant_from_payload(payload)
    event_id = (
        await db.execute(
            select(ConnectorInboundEvent.id).where(ConnectorInboundEvent.config_id == row.id, ConnectorInboundEvent.external_event_id == str(payload["id"]))
        )
    ).scalar_one_or_none()
    await ingest_one(ctx, db, row.installation_id, row.provider_key, applicant, inbound_event_id=event_id)
    if event_id is not None:
        ingest = (
            await db.execute(select(BoardApplicantIngest).where(BoardApplicantIngest.board_key == row.provider_key, BoardApplicantIngest.board_application_id == applicant.board_application_id))
        ).scalar_one_or_none()
        minimal = dict(payload)
        app_part = dict(minimal.get("applicant") or {})
        if isinstance(app_part.get("resume"), dict):
            app_part["resume"] = {"file": {"data": f"<stored:{ingest.status if ingest else 'none'}>"}}
        minimal["applicant"] = app_part
        await db.execute(update(ConnectorInboundEvent).where(ConnectorInboundEvent.id == event_id).values(payload=minimal))

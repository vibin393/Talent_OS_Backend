# ruff: noqa: E501
"""Recruitment marketing (Epic 39, FR-196..FR-201): ``/api/v1/marketing/*``.

Permissions: ``campaign:read`` (lists, previews, analytics), ``campaign:write`` (audiences, campaign drafts, submit),
``campaign:send`` (schedule below the approval threshold, pause / resume), ``sequence:write`` (sequences and
enrolment), ``marketing:configure`` (settings, tenant resume). Internal Users only (portal principals 401/404).
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from sqlalchemy import select

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.campaigns import (
    AudienceCreate,
    AudienceOut,
    AudiencePreviewOut,
    AudienceUpdate,
    CampaignCreate,
    CampaignMessagesPage,
    CampaignOut,
    CampaignSubmitIn,
    CampaignUpdate,
    EnrolIn,
    EnrollmentOut,
    EnrolOut,
    FunnelOut,
    ListEntriesIn,
    ListImportOut,
    MarketingOptionsOut,
    MarketingSettingsOut,
    MarketingSettingsUpdate,
    Option,
    PublishIn,
    ReasonIn,
    SequenceCreate,
    SequenceOut,
    SequenceUpdate,
    TestSendOut,
)
from app.services import files_service
from app.services.campaigns import analytics, audiences, campaigns, sequences
from app.services.campaigns import settings as mkt_settings

router = APIRouter(prefix="/marketing", tags=["marketing"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("campaign:read"))]
Write = Annotated[Principal, Depends(require("campaign:write"))]
Send = Annotated[Principal, Depends(require("campaign:send"))]
SeqWrite = Annotated[Principal, Depends(require("sequence:write"))]
Configure = Annotated[Principal, Depends(require("marketing:configure"))]
ChannelQ = Literal["email", "sms", "whatsapp"]
MAX_IMPORT_MB = 10


# ================================================================ settings
@router.get("/settings", response_model=MarketingSettingsOut)
async def get_settings(_: Read, db: DB) -> MarketingSettingsOut:
    return await mkt_settings.get_settings_out(get_context(), db)


@router.patch("/settings", response_model=MarketingSettingsOut)
async def update_settings(body: MarketingSettingsUpdate, _: Configure, db: DB) -> MarketingSettingsOut:
    return await mkt_settings.update_settings(get_context(), db, body)


@router.post("/settings/pause", response_model=MarketingSettingsOut)
async def pause_marketing(body: ReasonIn, _: Configure, db: DB) -> MarketingSettingsOut:
    return await mkt_settings.pause(get_context(), db, body.reason)


@router.post("/settings/resume", response_model=MarketingSettingsOut)
async def resume_marketing(body: ReasonIn, _: Configure, db: DB) -> MarketingSettingsOut:
    return await mkt_settings.resume(get_context(), db, body.reason)


@router.get("/options", response_model=MarketingOptionsOut)
async def options(_: Read, db: DB) -> MarketingOptionsOut:
    from app.models.campaigns import Sequence
    from app.models.comms import ChannelTemplate
    from app.models.demands import Demand, DemandPosting
    from app.models.talent_pools import Segment, TalentPool

    pools = (
        await db.execute(
            select(TalentPool.id, TalentPool.name, TalentPool.kind)
            .where(TalentPool.status == "active")
            .order_by(TalentPool.name)
            .limit(500)
        )
    ).all()
    segs = (
        await db.execute(
            select(Segment.id, Segment.name)
            .where(Segment.archived_at.is_(None))
            .order_by(Segment.name)
            .limit(500)
        )
    ).all()
    tpls = (
        await db.execute(
            select(ChannelTemplate.id, ChannelTemplate.name, ChannelTemplate.channel).where(
                ChannelTemplate.purpose == "marketing", ChannelTemplate.status == "registered"
            )
        )
    ).all()
    posts = (
        await db.execute(
            select(DemandPosting.id, Demand.title)
            .join(Demand, Demand.id == DemandPosting.demand_id)
            .where(DemandPosting.is_live.is_(True), Demand.status == "open", Demand.visibility != "internal")
            .order_by(Demand.title)
            .limit(200)
        )
    ).all()
    seqs = (
        await db.execute(
            select(Sequence.id, Sequence.name).where(Sequence.status == "active").order_by(Sequence.name)
        )
    ).all()
    return MarketingOptionsOut(
        pools=[Option(id=i, name=n, detail=k) for i, n, k in pools],
        segments=[Option(id=i, name=n) for i, n in segs],
        sms_templates=[Option(id=i, name=n) for i, n, c in tpls if c == "sms"],
        whatsapp_templates=[Option(id=i, name=n) for i, n, c in tpls if c == "whatsapp"],
        postings=[Option(id=i, name=t) for i, t in posts],
        sequences=[Option(id=i, name=n) for i, n in seqs],
    )


@router.get("/overview", response_model=FunnelOut)
async def overview(
    _: Read,
    db: DB,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> FunnelOut:
    return await analytics.overview(db, from_, to)


# ================================================================ audiences
@router.get("/audiences", response_model=list[AudienceOut])
async def list_audiences(
    _: Read, db: DB, include_archived: Annotated[bool, Query()] = False
) -> list[AudienceOut]:
    return await audiences.list_audiences(get_context(), db, include_archived)


@router.post("/audiences", response_model=AudienceOut, status_code=status.HTTP_201_CREATED)
async def create_audience(body: AudienceCreate, _: Write, db: DB) -> AudienceOut:
    return await audiences.create_audience(get_context(), db, body)


@router.get("/audiences/{audience_id}", response_model=AudienceOut)
async def get_audience(audience_id: uuid.UUID, _: Read, db: DB) -> AudienceOut:
    return await audiences.get_audience(get_context(), db, audience_id)


@router.patch("/audiences/{audience_id}", response_model=AudienceOut)
async def update_audience(audience_id: uuid.UUID, body: AudienceUpdate, _: Write, db: DB) -> AudienceOut:
    return await audiences.update_audience(get_context(), db, audience_id, body)


@router.delete("/audiences/{audience_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_audience(audience_id: uuid.UUID, _: Write, db: DB) -> Response:
    await audiences.archive_audience(get_context(), db, audience_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/audiences/{audience_id}/preview", response_model=AudiencePreviewOut)
async def preview_audience(
    audience_id: uuid.UUID, _: Read, db: DB, channel: Annotated[ChannelQ | None, Query()] = None
) -> AudiencePreviewOut:
    return await audiences.preview(get_context(), db, audience_id, channel)


@router.post("/audiences/{audience_id}/list-import", response_model=ListImportOut)
async def list_import(
    audience_id: uuid.UUID,
    _: Write,
    db: DB,
    file: Annotated[UploadFile, File()],
    mode: Annotated[Literal["include", "exclude"], Query()] = "include",
    apply: Annotated[bool, Query()] = False,
) -> ListImportOut:
    content = await files_service.read_upload(file, MAX_IMPORT_MB)
    return await audiences.list_import(
        get_context(), db, audience_id, file.filename, content, mode=mode, apply=apply
    )


@router.post("/audiences/{audience_id}/list-entries", response_model=AudienceOut)
async def add_list_entries(audience_id: uuid.UUID, body: ListEntriesIn, _: Write, db: DB) -> AudienceOut:
    return await audiences.add_list_entries(get_context(), db, audience_id, body)


@router.delete(
    "/audiences/{audience_id}/list-entries/{mode}/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_list_entry(
    audience_id: uuid.UUID, mode: Literal["include", "exclude"], candidate_id: uuid.UUID, _: Write, db: DB
) -> Response:
    await audiences.remove_list_entry(get_context(), db, audience_id, mode, candidate_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ================================================================ campaigns
@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    _: Read, db: DB, status_: Annotated[str | None, Query(alias="status", max_length=20)] = None
) -> list[CampaignOut]:
    return await campaigns.list_campaigns(get_context(), db, status_)


@router.post("/campaigns", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(body: CampaignCreate, _: Write, db: DB) -> CampaignOut:
    return await campaigns.create_campaign(get_context(), db, body)


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: uuid.UUID, _: Read, db: DB) -> CampaignOut:
    return await campaigns.get_campaign(get_context(), db, campaign_id)


@router.patch("/campaigns/{campaign_id}", response_model=CampaignOut)
async def update_campaign(campaign_id: uuid.UUID, body: CampaignUpdate, _: Write, db: DB) -> CampaignOut:
    return await campaigns.update_campaign(get_context(), db, campaign_id, body)


@router.post("/campaigns/{campaign_id}/submit", response_model=CampaignOut)
async def submit_campaign(
    campaign_id: uuid.UUID, _: Write, db: DB, body: CampaignSubmitIn | None = None
) -> CampaignOut:
    return await campaigns.submit(get_context(), db, campaign_id, body or CampaignSubmitIn())


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(campaign_id: uuid.UUID, _: Send, db: DB) -> CampaignOut:
    return await campaigns.pause(get_context(), db, campaign_id)


@router.post("/campaigns/{campaign_id}/resume", response_model=CampaignOut)
async def resume_campaign(campaign_id: uuid.UUID, _: Send, db: DB) -> CampaignOut:
    return await campaigns.resume(get_context(), db, campaign_id)


@router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(campaign_id: uuid.UUID, body: ReasonIn, _: Write, db: DB) -> CampaignOut:
    return await campaigns.cancel(get_context(), db, campaign_id, body.reason)


@router.post("/campaigns/{campaign_id}/test-send", response_model=TestSendOut)
async def test_send(campaign_id: uuid.UUID, _: Write, db: DB) -> TestSendOut:
    return await campaigns.test_send(get_context(), db, campaign_id)


@router.get("/campaigns/{campaign_id}/messages", response_model=CampaignMessagesPage)
async def campaign_messages(
    campaign_id: uuid.UUID,
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status", max_length=12)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CampaignMessagesPage:
    return await campaigns.list_messages(get_context(), db, campaign_id, status_, page, page_size)


@router.get("/campaigns/{campaign_id}/analytics", response_model=FunnelOut)
async def campaign_analytics(campaign_id: uuid.UUID, _: Read, db: DB) -> FunnelOut:
    return await analytics.campaign_funnel(db, campaign_id)


# ================================================================ sequences
@router.get("/sequences", response_model=list[SequenceOut])
async def list_sequences(
    _: Read, db: DB, status_: Annotated[str | None, Query(alias="status", max_length=10)] = None
) -> list[SequenceOut]:
    return await sequences.list_sequences(get_context(), db, status_)


@router.post("/sequences", response_model=SequenceOut, status_code=status.HTTP_201_CREATED)
async def create_sequence(body: SequenceCreate, _: Write, db: DB) -> SequenceOut:
    return await sequences.create_sequence(get_context(), db, body)


@router.get("/sequences/{sequence_id}", response_model=SequenceOut)
async def get_sequence(sequence_id: uuid.UUID, _: Read, db: DB) -> SequenceOut:
    return await sequences.get_sequence(get_context(), db, sequence_id)


@router.patch("/sequences/{sequence_id}", response_model=SequenceOut)
async def update_sequence(sequence_id: uuid.UUID, body: SequenceUpdate, _: Write, db: DB) -> SequenceOut:
    return await sequences.update_sequence(get_context(), db, sequence_id, body)


@router.post("/sequences/{sequence_id}/publish", response_model=SequenceOut)
async def publish_sequence(
    sequence_id: uuid.UUID, _: Write, db: DB, body: PublishIn | None = None
) -> SequenceOut:
    return await sequences.publish(get_context(), db, sequence_id, (body or PublishIn()).note)


@router.post("/sequences/{sequence_id}/enrol", response_model=EnrolOut)
async def enrol(sequence_id: uuid.UUID, body: EnrolIn, _: SeqWrite, db: DB) -> EnrolOut:
    return await sequences.enrol(
        get_context(), db, sequence_id, body.candidate_ids, origin="user", audience_id=body.audience_id
    )


@router.get("/sequences/{sequence_id}/enrollments", response_model=list[EnrollmentOut])
async def list_enrollments(
    sequence_id: uuid.UUID,
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status", max_length=12)] = None,
) -> list[EnrollmentOut]:
    return await sequences.list_enrollments(get_context(), db, sequence_id, status_)


@router.get("/sequences/{sequence_id}/analytics", response_model=FunnelOut)
async def sequence_analytics(sequence_id: uuid.UUID, _: Read, db: DB) -> FunnelOut:
    await sequences.get_sequence(get_context(), db, sequence_id)
    return await analytics.sequence_funnel(db, sequence_id)


@router.post("/sequences/{sequence_id}/{action}", response_model=SequenceOut)
async def sequence_action(
    sequence_id: uuid.UUID, action: Literal["pause", "resume", "archive"], _: Write, db: DB
) -> SequenceOut:
    return await sequences.set_status(get_context(), db, sequence_id, action)


@router.post("/enrollments/{enrollment_id}/{action}", response_model=EnrollmentOut)
async def enrollment_action(
    enrollment_id: uuid.UUID, action: Literal["pause", "resume", "exit"], _: SeqWrite, db: DB
) -> EnrollmentOut:
    return await sequences.enrollment_action(get_context(), db, enrollment_id, action)

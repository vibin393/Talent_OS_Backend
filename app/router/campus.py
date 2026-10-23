# ruff: file-ignore[line-too-long]
"""Campus hiring & recruitment events (Epic 43, FR-217..FR-221): ``/api/v1/campus/*`` (internal Users only; external
principals are refused by ``require``). Each endpoint calls one service function.

* Institutions + placement officers, drives (links, QR, demands, form, eligibility), registrations (override is
  human only, star), placement-officer import, assessments bulk invite, shortlist propose / confirm (star), interview
  batches + auto-slot, bulk offers / joining / renege, funnel -- ``campus:read`` / ``campus:manage``.
* Recruitment events, stations, check-in (kiosk), queue (poll <= 5 s, ``ETag`` / ``If-None-Match`` -> 304,
  ``Cache-Control: no-store``) and the event dashboard -- ``event:manage``.
* Settings -- ``campus:manage`` (audited).
"""

import hashlib
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile, status

from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.campus import (
    AutoSlotOut,
    BatchIn,
    BatchOut,
    BulkOffersIn,
    BulkOffersOut,
    CampusSettingsIn,
    CampusSettingsOut,
    CheckInIn,
    CheckinOut,
    ConfirmIn,
    ConfirmOut,
    ContactIn,
    ContactOut,
    ContactPatch,
    DriveDemandIn,
    DriveIn,
    DriveInstitutionIn,
    DriveInstitutionPatch,
    DriveListItem,
    DriveOut,
    DrivePatch,
    EligibilityIn,
    EventCheckinsOut,
    EventDashboardOut,
    EventIn,
    EventOut,
    EventPatch,
    FunnelOut,
    ImportApplyOut,
    ImportPreviewOut,
    InstitutionDetailOut,
    InstitutionIn,
    InstitutionOut,
    InstitutionPatch,
    InviteIn,
    InviteOut,
    JoiningRow,
    MoveSlotIn,
    NoteIn,
    OverrideIn,
    ProposalOut,
    ProposeIn,
    QueueEntryOut,
    QueueOut,
    ReasonIn,
    RegistrationOut,
    RegistrationPage,
    RenegeIn,
    StationIn,
    StationOut,
    StationPatch,
)
from app.services import files_service
from app.services.campus import (
    assessments,
    batches,
    common,
    drives,
    eligibility,
    events,
    funnel,
    imports,
    institutions,
    links,
    offers,
    queue,
    registration,
    shortlist,
)

router = APIRouter(prefix="/campus", tags=["campus"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("campus:read"))]
Manage = Annotated[Principal, Depends(require("campus:manage"))]
Override = Annotated[Principal, Depends(require("campus_eligibility:override"))]
Events = Annotated[Principal, Depends(require("event:manage"))]
EventsRead = Annotated[Principal, Depends(require_any("event:manage", "campus:read"))]
OfferCreate = Annotated[Principal, Depends(require("offer:create"))]
Invite = Annotated[Principal, Depends(require("assessment:invite"))]
NO_STORE = {"Cache-Control": "no-store"}


# ================================================================ settings
@router.get("/settings", response_model=CampusSettingsOut)
async def get_settings(_: Read, db: DB) -> Any:
    return await common.settings_out(db)


@router.put("/settings", response_model=CampusSettingsOut)
async def put_settings(_: Manage, db: DB, body: CampusSettingsIn) -> Any:
    return await common.update_settings(db, body)


# ================================================================ institutions / placement officers
@router.get("/institutions", response_model=list[InstitutionOut])
async def list_institutions(
    _: Read,
    db: DB,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status_: Annotated[str | None, Query(alias="status", pattern="^(active|archived)$")] = None,
    tier: Annotated[str | None, Query(pattern="^(tier_1|tier_2|tier_3|other)$")] = None,
) -> Any:
    return await institutions.list_institutions(db, q=q, status=status_, tier=tier)


@router.post("/institutions", response_model=InstitutionDetailOut, status_code=status.HTTP_201_CREATED)
async def create_institution(_: Manage, db: DB, body: InstitutionIn) -> Any:
    return await institutions.create_institution(db, body)


@router.get("/institutions/{institution_id}", response_model=InstitutionDetailOut)
async def get_institution(_: Read, db: DB, institution_id: uuid.UUID) -> Any:
    return await institutions.get_institution(db, institution_id)


@router.patch("/institutions/{institution_id}", response_model=InstitutionDetailOut)
async def update_institution(_: Manage, db: DB, institution_id: uuid.UUID, body: InstitutionPatch) -> Any:
    return await institutions.update_institution(db, institution_id, body)


@router.delete("/institutions/{institution_id}", response_model=InstitutionDetailOut)
async def archive_institution(_: Manage, db: DB, institution_id: uuid.UUID) -> Any:
    return await institutions.archive_institution(db, institution_id)


@router.get("/institutions/{institution_id}/contacts", response_model=list[ContactOut])
async def list_contacts(
    _: Read, db: DB, institution_id: uuid.UUID, include_archived: bool = False
) -> Any:
    return await institutions.list_contacts(db, institution_id, include_archived=include_archived)


@router.post(
    "/institutions/{institution_id}/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED
)
async def add_contact(_: Manage, db: DB, institution_id: uuid.UUID, body: ContactIn) -> Any:
    return await institutions.add_contact(db, institution_id, body)


@router.patch("/institutions/{institution_id}/contacts/{contact_id}", response_model=ContactOut)
async def update_contact(
    _: Manage, db: DB, institution_id: uuid.UUID, contact_id: uuid.UUID, body: ContactPatch
) -> Any:
    return await institutions.update_contact(db, institution_id, contact_id, body)


@router.delete("/institutions/{institution_id}/contacts/{contact_id}", response_model=ContactOut)
async def archive_contact(_: Manage, db: DB, institution_id: uuid.UUID, contact_id: uuid.UUID) -> Any:
    return await institutions.archive_contact(db, institution_id, contact_id)


# ================================================================ drives
@router.get("/drives", response_model=list[DriveListItem])
async def list_drives(
    p: Read,
    db: DB,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status_: Annotated[str | None, Query(alias="status", max_length=20)] = None,
) -> Any:
    return await drives.list_drives(common.ctx(p), db, q=q, status=status_)


@router.post("/drives", response_model=DriveOut, status_code=status.HTTP_201_CREATED)
async def create_drive(p: Manage, db: DB, body: DriveIn) -> Any:
    return await drives.create_drive(common.ctx(p), db, body)


@router.get("/drives/{drive_id}", response_model=DriveOut)
async def get_drive(p: Read, db: DB, drive_id: uuid.UUID) -> Any:
    return await drives.get_drive(common.ctx(p), db, drive_id)


@router.patch("/drives/{drive_id}", response_model=DriveOut)
async def update_drive(p: Manage, db: DB, drive_id: uuid.UUID, body: DrivePatch) -> Any:
    return await drives.update_drive(common.ctx(p), db, drive_id, body)


@router.post("/drives/{drive_id}/institutions", response_model=DriveOut, status_code=status.HTTP_201_CREATED)
async def add_drive_institution(p: Manage, db: DB, drive_id: uuid.UUID, body: DriveInstitutionIn) -> Any:
    return await drives.add_institution(common.ctx(p), db, drive_id, body)


@router.patch("/drives/{drive_id}/institutions/{drive_institution_id}", response_model=DriveOut)
async def update_drive_institution(
    p: Manage, db: DB, drive_id: uuid.UUID, drive_institution_id: uuid.UUID, body: DriveInstitutionPatch
) -> Any:
    return await drives.update_institution(common.ctx(p), db, drive_id, drive_institution_id, body)


@router.delete("/drives/{drive_id}/institutions/{drive_institution_id}", response_model=DriveOut)
async def remove_drive_institution(
    p: Manage, db: DB, drive_id: uuid.UUID, drive_institution_id: uuid.UUID
) -> Any:
    return await drives.remove_institution(common.ctx(p), db, drive_id, drive_institution_id)


@router.post("/drives/{drive_id}/demands", response_model=DriveOut, status_code=status.HTTP_201_CREATED)
async def add_drive_demand(p: Manage, db: DB, drive_id: uuid.UUID, body: DriveDemandIn) -> Any:
    return await drives.add_demand(common.ctx(p), db, drive_id, body.demand_id)


@router.delete("/drives/{drive_id}/demands/{demand_id}", response_model=DriveOut)
async def remove_drive_demand(p: Manage, db: DB, drive_id: uuid.UUID, demand_id: uuid.UUID) -> Any:
    return await drives.remove_demand(common.ctx(p), db, drive_id, demand_id)


@router.post("/drives/{drive_id}/open-registration", response_model=DriveOut)
async def open_registration(p: Manage, db: DB, drive_id: uuid.UUID) -> Any:
    return await drives.open_registration(common.ctx(p), db, drive_id)


@router.post("/drives/{drive_id}/close-registration", response_model=DriveOut)
async def close_registration(p: Manage, db: DB, drive_id: uuid.UUID) -> Any:
    return await drives.close_registration(common.ctx(p), db, drive_id)


@router.post("/drives/{drive_id}/close", response_model=DriveOut)
async def close_drive(p: Manage, db: DB, drive_id: uuid.UUID) -> Any:
    return await drives.close_drive(common.ctx(p), db, drive_id)


@router.post("/drives/{drive_id}/cancel", response_model=DriveOut)
async def cancel_drive(p: Manage, db: DB, drive_id: uuid.UUID, body: ReasonIn) -> Any:
    return await drives.cancel_drive(common.ctx(p), db, drive_id, body.reason)


@router.get("/drives/{drive_id}/institutions/{drive_institution_id}/qr")
async def drive_qr(
    p: Read,
    db: DB,
    drive_id: uuid.UUID,
    drive_institution_id: uuid.UUID,
    format_: Annotated[str, Query(alias="format", pattern="^(png|svg|pdf)$")] = "png",
) -> Response:
    data, media_type, filename = await links.qr_download(common.ctx(p), db, drive_id, drive_institution_id, format_)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', **NO_STORE},
    )


# ---------------------------------------------------------------- eligibility
@router.get("/eligibility/fields")
async def eligibility_fields(p: Read) -> Any:
    return eligibility.field_catalogue(p)


@router.put("/drives/{drive_id}/eligibility", response_model=DriveOut)
async def set_eligibility(p: Manage, db: DB, drive_id: uuid.UUID, body: EligibilityIn) -> Any:
    return await eligibility.set_definition(common.ctx(p), db, drive_id, body.filters)


@router.post("/registrations/{registration_id}/override-eligibility", response_model=RegistrationOut)
async def override_eligibility(p: Override, db: DB, registration_id: uuid.UUID, body: OverrideIn) -> Any:
    return await eligibility.override(common.ctx(p), db, registration_id, body.reason)


# ---------------------------------------------------------------- registrations
@router.get("/drives/{drive_id}/registrations", response_model=RegistrationPage)
async def list_registrations(
    p: Read,
    db: DB,
    drive_id: uuid.UUID,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    institution_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    flagged: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    return await registration.list_registrations(
        common.ctx(p), db, drive_id, statuses=status_, institution_id=institution_id, q=q, flagged=flagged,
        limit=limit, offset=offset,
    )


@router.get("/registrations/{registration_id}", response_model=RegistrationOut)
async def get_registration(p: Read, db: DB, registration_id: uuid.UUID) -> Any:
    return await registration.get_registration(common.ctx(p), db, registration_id)


@router.post("/registrations/{registration_id}/withdraw", response_model=RegistrationOut)
async def withdraw_registration(p: Manage, db: DB, registration_id: uuid.UUID, body: NoteIn) -> Any:
    return await registration.withdraw(common.ctx(p), db, registration_id, body.note)


@router.post("/registrations/{registration_id}/renege", response_model=RegistrationOut)
async def renege(p: Manage, db: DB, registration_id: uuid.UUID, body: RenegeIn) -> Any:
    return await offers.renege(common.ctx(p), db, registration_id, body.note)


# ---------------------------------------------------------------- placement-officer import
@router.post("/drives/{drive_id}/imports/preview", response_model=ImportPreviewOut)
async def import_preview(
    p: Manage,
    db: DB,
    drive_id: uuid.UUID,
    drive_institution_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
) -> Any:
    data = await files_service.read_upload(file, 5)
    return await imports.preview(common.ctx(p), db, drive_id, drive_institution_id, data, file.filename)


@router.post("/drives/{drive_id}/imports/{batch_id}/apply", response_model=ImportApplyOut)
async def import_apply(p: Manage, db: DB, drive_id: uuid.UUID, batch_id: uuid.UUID) -> Any:
    return await imports.apply(common.ctx(p), db, drive_id, batch_id)


# ---------------------------------------------------------------- assessments / shortlist
@router.post("/drives/{drive_id}/assessments/invite", response_model=InviteOut)
async def invite_assessments(p: Manage, _i: Invite, db: DB, drive_id: uuid.UUID, body: InviteIn) -> Any:
    return await assessments.invite(common.ctx(p), db, drive_id, body)


@router.post("/drives/{drive_id}/shortlist/propose", response_model=ProposalOut)
async def propose_shortlist(p: Manage, db: DB, drive_id: uuid.UUID, body: ProposeIn) -> Any:
    return await shortlist.propose(common.ctx(p), db, drive_id, body)


@router.post("/drives/{drive_id}/shortlist/confirm", response_model=ConfirmOut)
async def confirm_shortlist(p: Manage, db: DB, drive_id: uuid.UUID, body: ConfirmIn) -> Any:
    return await shortlist.confirm(common.ctx(p), db, drive_id, body)


# ---------------------------------------------------------------- interview batches
@router.get("/drives/{drive_id}/batches", response_model=list[BatchOut])
async def list_batches(p: Read, db: DB, drive_id: uuid.UUID) -> Any:
    return await batches.list_batches(common.ctx(p), db, drive_id)


@router.post("/drives/{drive_id}/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def create_batch(p: Manage, db: DB, drive_id: uuid.UUID, body: BatchIn) -> Any:
    return await batches.create_batch(common.ctx(p), db, drive_id, body)


@router.get("/batches/{batch_id}", response_model=BatchOut)
async def get_batch(p: Read, db: DB, batch_id: uuid.UUID) -> Any:
    return await batches.get_batch(common.ctx(p), db, batch_id)


@router.post("/batches/{batch_id}/auto-slot", response_model=AutoSlotOut)
async def auto_slot(p: Manage, db: DB, batch_id: uuid.UUID) -> Any:
    return await batches.auto_slot(common.ctx(p), db, batch_id)


@router.post("/batches/{batch_id}/slots/{slot_id}/move", response_model=BatchOut)
async def move_slot(p: Manage, db: DB, batch_id: uuid.UUID, slot_id: uuid.UUID, body: MoveSlotIn) -> Any:
    return await batches.move_slot(common.ctx(p), db, batch_id, slot_id, body.to_slot_id)


@router.post("/batches/{batch_id}/cancel", response_model=BatchOut)
async def cancel_batch(p: Manage, db: DB, batch_id: uuid.UUID) -> Any:
    return await batches.cancel_batch(common.ctx(p), db, batch_id)


# ---------------------------------------------------------------- offers / joining / funnel
@router.post("/drives/{drive_id}/offers", response_model=BulkOffersOut)
async def bulk_offers(p: OfferCreate, _r: Read, db: DB, drive_id: uuid.UUID, body: BulkOffersIn) -> Any:
    return await offers.bulk_create(common.ctx(p), db, drive_id, body)


@router.get("/drives/{drive_id}/offers", response_model=list[JoiningRow])
async def joining(p: Read, db: DB, drive_id: uuid.UUID) -> Any:
    return await offers.joining(common.ctx(p), db, drive_id)


@router.get("/offer-withdrawal-reasons", response_model=list[str])
async def withdrawal_reasons(_: Read) -> Any:
    return offers.withdrawal_reasons()


@router.get("/drives/{drive_id}/funnel", response_model=FunnelOut)
async def drive_funnel(p: Read, db: DB, drive_id: uuid.UUID) -> Any:
    return await funnel.funnel(common.ctx(p), db, drive_id, None, None)


@router.get("/funnel", response_model=FunnelOut)
async def tenant_funnel(
    p: Read, db: DB, from_: Annotated[date | None, Query(alias="from")] = None, to: date | None = None
) -> Any:
    return await funnel.funnel(common.ctx(p), db, None, from_, to)


# ================================================================ recruitment events
@router.get("/events", response_model=list[EventOut])
async def list_events(
    p: EventsRead, db: DB, status_: Annotated[str | None, Query(alias="status", max_length=10)] = None
) -> Any:
    return await events.list_events(common.ctx(p), db, status=status_)


@router.post("/events", response_model=EventOut, status_code=status.HTTP_201_CREATED)
async def create_event(p: Events, db: DB, body: EventIn) -> Any:
    return await events.create_event(common.ctx(p), db, body)


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(p: EventsRead, db: DB, event_id: uuid.UUID) -> Any:
    return await events.get_event(common.ctx(p), db, event_id)


@router.patch("/events/{event_id}", response_model=EventOut)
async def update_event(p: Events, db: DB, event_id: uuid.UUID, body: EventPatch) -> Any:
    return await events.update_event(common.ctx(p), db, event_id, body)


@router.post("/events/{event_id}/open", response_model=EventOut)
async def open_event(p: Events, db: DB, event_id: uuid.UUID) -> Any:
    return await events.transition(common.ctx(p), db, event_id, "open")


@router.post("/events/{event_id}/go-live", response_model=EventOut)
async def go_live(p: Events, db: DB, event_id: uuid.UUID) -> Any:
    return await events.transition(common.ctx(p), db, event_id, "live")


@router.post("/events/{event_id}/close", response_model=EventOut)
async def close_event(p: Events, db: DB, event_id: uuid.UUID) -> Any:
    return await events.transition(common.ctx(p), db, event_id, "closed")


@router.post("/events/{event_id}/cancel", response_model=EventOut)
async def cancel_event(p: Events, db: DB, event_id: uuid.UUID, body: ReasonIn) -> Any:
    return await events.transition(common.ctx(p), db, event_id, "cancelled", reason=body.reason)


@router.get("/events/{event_id}/qr")
async def event_qr(
    p: Events,
    db: DB,
    event_id: uuid.UUID,
    format_: Annotated[str, Query(alias="format", pattern="^(png|svg|pdf)$")] = "png",
) -> Response:
    data, media_type, filename = await links.event_qr_download(common.ctx(p), db, event_id, format_)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', **NO_STORE},
    )


@router.get("/events/{event_id}/stations", response_model=list[StationOut])
async def list_stations(p: Events, db: DB, event_id: uuid.UUID) -> Any:
    return await events.list_stations(common.ctx(p), db, event_id)


@router.post("/events/{event_id}/stations", response_model=StationOut, status_code=status.HTTP_201_CREATED)
async def add_station(p: Events, db: DB, event_id: uuid.UUID, body: StationIn) -> Any:
    return await events.add_station(common.ctx(p), db, event_id, body)


@router.patch("/events/{event_id}/stations/{station_id}", response_model=StationOut)
async def update_station(p: Events, db: DB, event_id: uuid.UUID, station_id: uuid.UUID, body: StationPatch) -> Any:
    return await events.update_station(common.ctx(p), db, event_id, station_id, body)


@router.get("/events/{event_id}/checkins", response_model=EventCheckinsOut)
async def list_checkins(
    p: Events,
    db: DB,
    event_id: uuid.UUID,
    status_: Annotated[str | None, Query(alias="status", max_length=12)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    return await events.list_checkins(common.ctx(p), db, event_id, status=status_, q=q, limit=limit, offset=offset)


@router.post("/events/{event_id}/check-in", response_model=CheckinOut)
async def check_in(p: Events, db: DB, event_id: uuid.UUID, body: CheckInIn) -> Any:
    return await events.check_in(common.ctx(p), db, event_id, body)


@router.get("/events/{event_id}/queue", response_model=QueueOut)
async def get_queue(
    p: Events,
    db: DB,
    event_id: uuid.UUID,
    response: Response,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Any:
    out = await queue.queue_view(common.ctx(p), db, event_id)
    etag = '"' + hashlib.sha256(out.model_dump_json(exclude={"generated_at"}).encode()).hexdigest()[:32] + '"'
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag, **NO_STORE})
    response.headers["ETag"] = etag
    response.headers.update(NO_STORE)
    return out


@router.post("/events/{event_id}/stations/{station_id}/call-next", response_model=QueueEntryOut | None)
async def call_next(p: Events, db: DB, event_id: uuid.UUID, station_id: uuid.UUID) -> Any:
    return await queue.call_next(common.ctx(p), db, event_id, station_id)


@router.post("/events/{event_id}/queue/{entry_id}/start", response_model=QueueEntryOut)
async def start_entry(p: Events, db: DB, event_id: uuid.UUID, entry_id: uuid.UUID) -> Any:
    return await queue.start(common.ctx(p), db, event_id, entry_id)


@router.post("/events/{event_id}/queue/{entry_id}/complete", response_model=QueueEntryOut)
async def complete_entry(p: Events, db: DB, event_id: uuid.UUID, entry_id: uuid.UUID) -> Any:
    return await queue.finish(common.ctx(p), db, event_id, entry_id, "done")


@router.post("/events/{event_id}/queue/{entry_id}/skip", response_model=QueueEntryOut)
async def skip_entry(p: Events, db: DB, event_id: uuid.UUID, entry_id: uuid.UUID) -> Any:
    return await queue.finish(common.ctx(p), db, event_id, entry_id, "skipped")


@router.post("/events/{event_id}/queue/{entry_id}/left", response_model=QueueEntryOut)
async def left_entry(p: Events, db: DB, event_id: uuid.UUID, entry_id: uuid.UUID) -> Any:
    return await queue.finish(common.ctx(p), db, event_id, entry_id, "left")


@router.post("/events/{event_id}/queue/{entry_id}/requeue", response_model=QueueEntryOut)
async def requeue_entry(p: Events, db: DB, event_id: uuid.UUID, entry_id: uuid.UUID) -> Any:
    return await queue.requeue(common.ctx(p), db, event_id, entry_id)


@router.get("/events/{event_id}/dashboard", response_model=EventDashboardOut)
async def event_dashboard(p: Events, db: DB, event_id: uuid.UUID) -> Any:
    return await queue.dashboard(common.ctx(p), db, event_id)

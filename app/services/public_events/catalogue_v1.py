# ruff: noqa: E501
"""Public Event catalogue v1 (Epic 66, AD-79, spec §0.8; registered from ``hooks.py``).

Every ``build`` reads only fields present on the domain event (``_present``): a field the event lacks is omitted from
``data``, never looked up. Thin by construction (D-8): ids, enums, statuses, dates and changed field NAMES only.
Source names were verified against ``app/events/*.py`` (see the deviations noted per spec below).
"""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.public_events import DYNAMIC_SUBJECT, PublicEventSpec, register_public_event
from app.events.agents import ProposalDecided
from app.events.billing import InvoiceIssued
from app.events.candidates import CandidateCreated, CandidateUpdated
from app.events.demands import DemandCreated, DemandPublished, DemandStatusChanged, DemandUnpublished
from app.events.ecosystem import InstallationChanged
from app.events.interviews import InterviewCancelled, InterviewRescheduled, InterviewScheduled
from app.events.offers import OfferAccepted
from app.events.pipeline import ApplicationCreated, ApplicationStageChanged, SubmissionCreated
from app.events.placements import PlacementActivated, PlacementEnded
from app.events.privacy import DsrCompleted
from app.events.receivables import PaymentRecorded
from app.events.time import TimesheetApproved

_MISSING = object()


def _present(event: Any, **mapping: str) -> dict[str, Any]:
    """``{data_field: event.<attr>}`` for every mapped attribute the event class actually declares."""
    out: dict[str, Any] = {}
    for field, attr in mapping.items():
        value = getattr(event, attr, _MISSING)
        if value is not _MISSING:
            out[field] = value
    return out


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ data models (v1)
class CandidateCreatedData(_Data):
    id: uuid.UUID
    source: str | None = None  # CandidateCreated carries no ``source`` today: omitted


class CandidateUpdatedData(_Data):
    id: uuid.UUID
    changed_fields: list[str] | None = None  # CandidateUpdated carries no field names today: omitted


class SubjectErasedData(_Data):
    subject_type: str
    id: uuid.UUID


class DemandCreatedData(_Data):
    id: uuid.UUID
    client_id: uuid.UUID | None = None  # not on DemandCreated: omitted
    status: str | None = None  # not on DemandCreated: omitted


class DemandStatusChangedData(_Data):
    id: uuid.UUID
    from_status: str | None = None
    to_status: str


class PostingData(_Data):
    demand_id: uuid.UUID
    posting_id: uuid.UUID | None = None  # not on DemandPublished / DemandUnpublished: omitted


class ApplicationCreatedData(_Data):
    id: uuid.UUID
    candidate_id: uuid.UUID
    demand_id: uuid.UUID
    source: str | None = None


class ApplicationStageChangedData(_Data):
    id: uuid.UUID
    candidate_id: uuid.UUID
    demand_id: uuid.UUID
    from_stage: str | None = None
    to_stage: str


class SubmissionCreatedData(_Data):
    id: uuid.UUID
    application_id: uuid.UUID
    client_id: uuid.UUID


class InterviewData(_Data):
    id: uuid.UUID
    application_id: uuid.UUID
    starts_at: datetime | None = None  # omitted on cancel


class OfferAcceptedData(_Data):
    id: uuid.UUID
    application_id: uuid.UUID


class PlacementStartedData(_Data):
    id: uuid.UUID
    candidate_id: uuid.UUID
    client_id: uuid.UUID | None = None  # not on PlacementActivated: omitted
    start_date: date


class PlacementEndedData(_Data):
    id: uuid.UUID
    end_date: date
    end_kind: str | None = (
        None  # not on PlacementEnded (only the free-text ``reason``, never published): omitted
    )


class TimesheetApprovedData(_Data):
    id: uuid.UUID
    placement_id: uuid.UUID
    period_start: date | None = None  # not on TimesheetApproved: omitted
    period_end: date | None = None


class InvoiceIssuedData(_Data):
    id: uuid.UUID
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    number: str


class PaymentRecordedData(_Data):
    id: uuid.UUID
    client_id: uuid.UUID


class ProposalDecidedData(_Data):
    id: uuid.UUID
    tool_key: str
    outcome: str


class InstallationChangedData(_Data):
    installation_id: uuid.UUID
    change: str
    status: str


# ------------------------------------------------------------------ builders
def _candidate_created(e: Any) -> CandidateCreatedData:
    return CandidateCreatedData(**_present(e, id="candidate_id", source="source"))


def _candidate_updated(e: Any) -> CandidateUpdatedData:
    data = _present(e, id="candidate_id", changed_fields="changed_fields")
    if "changed_fields" in data:
        data["changed_fields"] = sorted(str(f) for f in data["changed_fields"] or ())
    return CandidateUpdatedData(**data)


ERASABLE_SUBJECTS = ("candidate", "worker")


def _erased_subject(e: Any) -> tuple[str, uuid.UUID] | None:
    """DsrCompleted carries ``dsr_id`` and ``kind`` only today; the subject is read only when the event declares it
    (``subject_kind`` / ``subject_type`` and ``subject_id`` / ``candidate_id``), never looked up."""
    kind = getattr(e, "subject_kind", None) or getattr(e, "subject_type", None)
    sid = getattr(e, "subject_id", None) or getattr(e, "candidate_id", None)
    if kind is None and getattr(e, "candidate_id", None) is not None:
        kind = "candidate"
    if kind not in ERASABLE_SUBJECTS or sid is None:
        return None
    return str(kind), uuid.UUID(str(sid))


def _subject_erased(e: Any) -> SubjectErasedData | None:
    if getattr(e, "kind", None) != "erasure":
        return None
    subject = _erased_subject(e)
    if subject is None:
        return None
    return SubjectErasedData(subject_type=subject[0], id=subject[1])


def _demand_created(e: Any) -> DemandCreatedData:
    return DemandCreatedData(**_present(e, id="demand_id", client_id="client_id", status="status"))


def _demand_status_changed(e: Any) -> DemandStatusChangedData:
    return DemandStatusChangedData(
        **_present(e, id="demand_id", from_status="from_status", to_status="to_status")
    )


def _posting(e: Any) -> PostingData:
    return PostingData(**_present(e, demand_id="demand_id", posting_id="posting_id"))


def _application_created(e: Any) -> ApplicationCreatedData:
    return ApplicationCreatedData(
        **_present(
            e, id="application_id", candidate_id="candidate_id", demand_id="demand_id", source="source"
        )
    )


def _application_stage_changed(e: Any) -> ApplicationStageChangedData:
    return ApplicationStageChangedData(
        **_present(
            e,
            id="application_id",
            candidate_id="candidate_id",
            demand_id="demand_id",
            from_stage="from_key",
            to_stage="to_key",
        )
    )


def _submission_created(e: Any) -> SubmissionCreatedData:
    return SubmissionCreatedData(
        **_present(e, id="submission_id", application_id="application_id", client_id="client_id")
    )


def _interview(e: Any) -> InterviewData:
    return InterviewData(
        **_present(e, id="interview_id", application_id="application_id", starts_at="starts_at")
    )


def _interview_cancelled(e: Any) -> InterviewData:
    return InterviewData(**_present(e, id="interview_id", application_id="application_id"))


def _offer_accepted(e: Any) -> OfferAcceptedData:
    return OfferAcceptedData(**_present(e, id="offer_id", application_id="application_id"))


def _placement_started(e: Any) -> PlacementStartedData:
    return PlacementStartedData(
        **_present(
            e,
            id="placement_id",
            candidate_id="candidate_id",
            client_id="client_id",
            start_date="actual_start_date",
        )
    )


def _placement_ended(e: Any) -> PlacementEndedData:
    return PlacementEndedData(**_present(e, id="placement_id", end_date="end_date", end_kind="end_kind"))


def _timesheet_approved(e: Any) -> TimesheetApprovedData:
    return TimesheetApprovedData(
        **_present(
            e,
            id="timesheet_id",
            placement_id="placement_id",
            period_start="period_start",
            period_end="period_end",
        )
    )


def _invoice_issued(e: Any) -> InvoiceIssuedData:
    return InvoiceIssuedData(
        **_present(
            e, id="invoice_id", client_id="client_id", legal_entity_id="legal_entity_id", number="number"
        )
    )


def _payment_recorded(e: Any) -> PaymentRecordedData:
    return PaymentRecordedData(**_present(e, id="payment_id", client_id="client_id"))


def _proposal_decided(e: Any) -> ProposalDecidedData:
    return ProposalDecidedData(**_present(e, id="proposal_id", tool_key="tool_key", outcome="outcome"))


def _installation_changed(e: Any) -> InstallationChangedData:
    return InstallationChangedData(
        **_present(e, installation_id="installation_id", change="change", status="status")
    )


def _erased_subject_id(e: Any) -> uuid.UUID:
    subject = _erased_subject(e)
    if subject is None:  # pragma: no cover - build returned None first
        msg = "subject.erased without a subject"
        raise ValueError(msg)
    return subject[1]


def _spec(type_: str, source: type, subject_type: str, subject_attr: str, model: type[BaseModel], build: Any,
          scopes: tuple[str, ...], description: str) -> PublicEventSpec:  # fmt: skip
    return PublicEventSpec(
        type=type_, version=1, source=source, subject_type=subject_type,
        subject_id=lambda e, _a=subject_attr: getattr(e, _a), data_model=model, build=build, scopes=scopes,
        description=description,
    )  # fmt: skip


def v1_specs() -> list[PublicEventSpec]:
    specs = [
        _spec("candidate.created", CandidateCreated, "candidate", "candidate_id", CandidateCreatedData, _candidate_created, ("candidates:read",), "A candidate was created."),
        _spec("candidate.updated", CandidateUpdated, "candidate", "candidate_id", CandidateUpdatedData, _candidate_updated, ("candidates:read",), "A candidate (or one of its child records) changed."),
        _spec("demand.created", DemandCreated, "demand", "demand_id", DemandCreatedData, _demand_created, ("demands:read",), "A demand (job requirement) was created."),
        _spec("demand.status_changed", DemandStatusChanged, "demand", "demand_id", DemandStatusChangedData, _demand_status_changed, ("demands:read",), "A demand changed status."),
        _spec("posting.published", DemandPublished, "demand", "demand_id", PostingData, _posting, ("postings:read", "demands:read"), "A demand was published to the careers site."),
        _spec("posting.closed", DemandUnpublished, "demand", "demand_id", PostingData, _posting, ("postings:read", "demands:read"), "A demand was unpublished."),
        _spec("application.created", ApplicationCreated, "application", "application_id", ApplicationCreatedData, _application_created, ("applications:read",), "A candidate entered a demand's pipeline."),
        _spec("application.stage_changed", ApplicationStageChanged, "application", "application_id", ApplicationStageChangedData, _application_stage_changed, ("applications:read",), "An application moved to another stage."),
        _spec("submission.created", SubmissionCreated, "submission", "submission_id", SubmissionCreatedData, _submission_created, ("applications:read",), "A candidate was submitted to a client."),
        _spec("interview.scheduled", InterviewScheduled, "interview", "interview_id", InterviewData, _interview, ("interviews:read",), "An interview was scheduled."),
        _spec("interview.rescheduled", InterviewRescheduled, "interview", "interview_id", InterviewData, _interview, ("interviews:read",), "An interview was rescheduled."),
        _spec("interview.cancelled", InterviewCancelled, "interview", "interview_id", InterviewData, _interview_cancelled, ("interviews:read",), "An interview was cancelled."),
        _spec("offer.accepted", OfferAccepted, "offer", "offer_id", OfferAcceptedData, _offer_accepted, ("offers:read",), "An offer was accepted."),
        _spec("placement.started", PlacementActivated, "placement", "placement_id", PlacementStartedData, _placement_started, ("placements:read",), "A placement started."),
        _spec("placement.ended", PlacementEnded, "placement", "placement_id", PlacementEndedData, _placement_ended, ("placements:read",), "A placement ended."),
        _spec("timesheet.approved", TimesheetApproved, "timesheet", "timesheet_id", TimesheetApprovedData, _timesheet_approved, ("timesheets:read",), "A timesheet was approved."),
        _spec("invoice.issued", InvoiceIssued, "invoice", "invoice_id", InvoiceIssuedData, _invoice_issued, ("invoices:read",), "An invoice was issued."),
        _spec("payment.recorded", PaymentRecorded, "payment", "payment_id", PaymentRecordedData, _payment_recorded, ("invoices:read",), "A client payment was recorded."),
        _spec("proposal.decided", ProposalDecided, "proposal", "proposal_id", ProposalDecidedData, _proposal_decided, ("mcp:read",), "An AI agent proposal was decided (delivered only to the Installation whose MCP session created it)."),
        _spec("app.installation_changed", InstallationChanged, "installation", "installation_id", InstallationChangedData, _installation_changed, (), "The Installation changed (always delivered to the Installation itself)."),
    ]  # fmt: skip
    specs.append(
        PublicEventSpec(
            type="subject.erased",
            version=1,
            source=DsrCompleted,
            subject_type=DYNAMIC_SUBJECT,
            subject_id=_erased_subject_id,
            data_model=SubjectErasedData,
            build=_subject_erased,
            scopes=(),
            description="A data subject (candidate or worker) was erased; delivered to every Installation that received "
            "the subject (D-11, subject_exposures), whatever its scopes.",
        )
    )
    return specs


def register_v1() -> None:
    from app.core.public_events import catalogue

    existing = catalogue(include_internal=True)
    for spec in v1_specs():
        if spec.type not in existing:
            register_public_event(spec)


__all__ = ["ERASABLE_SUBJECTS", "register_v1", "v1_specs"]

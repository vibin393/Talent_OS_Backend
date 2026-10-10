# ruff: noqa: E501
"""Epic 66 (66.4, AD-79): the transactional outbox, the Public Event catalogue v1, the relay and the envelope.

* one outbox row per mapped domain event (catalogue v1), written in the domain transaction;
* a rollback writes no row; a rolled-back savepoint drops its rows; ``build`` returning ``None`` writes nothing;
* the PII denylist validation (nested too) and the catalogue v1 validates;
* the canonical envelope bytes (golden);
* a crash between the outbox write and the relay loses nothing (the sweep recovers);
* a failing sink leaves the batch pending (its SAVEPOINT rolled back), the next run delivers.
"""

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import public_events as registry
from app.core.context import RequestContext, reset_context, set_context
from app.core.database import tenant_session
from app.core.events import emit
from app.core.permissions import Principal
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
from app.models.public_events import OutboxEvent
from app.schemas.public_events import EventQuery
from app.services import public_events_service
from app.services.public_events.outbox import PublicEventRow

V1_TYPES = {
    "candidate.created", "candidate.updated", "subject.erased", "demand.created", "demand.status_changed",
    "posting.published", "posting.closed", "application.created", "application.stage_changed", "submission.created",
    "interview.scheduled", "interview.rescheduled", "interview.cancelled", "offer.accepted", "placement.started",
    "placement.ended", "timesheet.approved", "invoice.issued", "payment.recorded", "proposal.decided",
    "app.installation_changed",
}  # fmt: skip


@dataclasses.dataclass(frozen=True, slots=True)
class DsrCompletedWithSubject(DsrCompleted):
    """What DsrCompleted carries once the privacy owner adds the subject (dispatch follows the MRO)."""

    subject_kind: str = "candidate"
    subject_id: uuid.UUID | None = None


def _u() -> uuid.UUID:
    return uuid.uuid4()


def mapped_events(t: uuid.UUID) -> list[tuple[str, object]]:
    now = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    return [
        ("candidate.created", CandidateCreated(t, _u())),
        ("candidate.updated", CandidateUpdated(t, _u())),
        ("subject.erased", DsrCompletedWithSubject(t, _u(), "erasure", "candidate", _u())),
        ("demand.created", DemandCreated(t, _u(), "user")),
        ("demand.status_changed", DemandStatusChanged(t, _u(), "draft", "open", "user", None)),
        ("posting.published", DemandPublished(t, _u(), _u())),
        ("posting.closed", DemandUnpublished(t, _u(), "user")),
        ("application.created", ApplicationCreated(t, _u(), _u(), _u(), "careers_site", "user")),
        ("application.stage_changed", ApplicationStageChanged(t, _u(), _u(), _u(), "applied", "screen", "user")),
        ("submission.created", SubmissionCreated(t, _u(), _u(), _u(), _u(), "draft", False)),
        ("interview.scheduled", InterviewScheduled(t, _u(), _u(), _u(), _u(), "Round 1", now)),
        ("interview.rescheduled", InterviewRescheduled(t, _u(), _u(), 2, now)),
        ("interview.cancelled", InterviewCancelled(t, _u(), _u(), 3)),
        ("offer.accepted", OfferAccepted(t, _u(), _u(), _u(), _u(), 1)),
        ("placement.started", PlacementActivated(t, _u(), _u(), date(2026, 11, 1))),
        ("placement.ended", PlacementEnded(t, _u(), _u(), date(2027, 1, 31), "Contract over", "contractor", True)),
        ("timesheet.approved", TimesheetApproved(t, _u(), _u())),
        ("invoice.issued", InvoiceIssued(t, _u(), _u(), _u(), "INV/26-27/0001", date(2026, 10, 9))),
        ("payment.recorded", PaymentRecorded(t, _u(), _u(), _u())),
        ("proposal.decided", ProposalDecided(t, _u(), None, "sourcing", "pipeline.move", "approved", "executed", None, None, None)),
        ("app.installation_changed", InstallationChanged(t, _u(), "connector", None, "fake_hrms", "installed", "active", "user")),
    ]  # fmt: skip


async def _rows(tenant: uuid.UUID) -> list[OutboxEvent]:
    async with tenant_session(tenant) as db:
        return list((await db.execute(select(OutboxEvent).order_by(OutboxEvent.seq))).scalars())


async def test_catalogue_v1_validates() -> None:
    assert set(registry.catalogue()) == V1_TYPES
    assert registry.validate_catalogue() == []
    assert registry.spec_for("application.stage_changed").scopes == ("applications:read",)
    for spec in registry.all_specs(include_internal=True):
        assert spec.data_model.model_config.get("extra") == "forbid"


async def test_one_row_per_mapped_event(tenant: uuid.UUID, no_kick: None) -> None:
    events = mapped_events(tenant)
    async with tenant_session(tenant) as db:
        for _, e in events:
            emit(db, e)
    rows = await _rows(tenant)
    assert [r.event_type for r in rows] == [t for t, _ in events]
    assert {r.event_type for r in rows} == V1_TYPES
    by_type = {r.event_type: r for r in rows}
    for r in rows:
        assert r.dispatch_status == "pending" and r.visibility == "public" and r.event_version == 1
        assert r.actor_kind == "system" and r.app_installation_id is None
    seqs = [r.seq for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    stage = by_type["application.stage_changed"]
    src = dict(events)["application.stage_changed"]
    assert stage.subject_type == "application" and stage.subject_id == src.application_id  # type: ignore[attr-defined]
    assert stage.data == {
        "id": str(src.application_id), "candidate_id": str(src.candidate_id), "demand_id": str(src.demand_id),  # type: ignore[attr-defined]
        "from_stage": "applied", "to_stage": "screen",
    }  # fmt: skip
    assert stage.source_event == "ApplicationStageChanged"
    # fields the source event lacks are omitted, never looked up
    assert set(by_type["candidate.created"].data) == {"id"}
    assert set(by_type["demand.created"].data) == {"id"}
    assert set(by_type["placement.started"].data) == {"id", "candidate_id", "start_date"}
    assert by_type["placement.started"].data["start_date"] == "2026-11-01"
    assert set(by_type["placement.ended"].data) == {"id", "end_date"}  # never the free-text ``reason``
    assert "starts_at" not in by_type["interview.cancelled"].data
    assert by_type["interview.scheduled"].data["starts_at"].startswith("2026-10-09T10:00:00")
    erased = by_type["subject.erased"]
    assert erased.subject_type == "candidate" and erased.data == {"subject_type": "candidate", "id": str(erased.subject_id)}
    assert by_type["app.installation_changed"].data["change"] == "installed"


async def test_build_none_writes_nothing(tenant: uuid.UUID, no_kick: None) -> None:
    async with tenant_session(tenant) as db:
        emit(db, DsrCompleted(tenant, _u(), "access"))  # not an erasure
        emit(db, DsrCompleted(tenant, _u(), "erasure"))  # the event names no subject (never looked up)
        emit(db, DsrCompletedWithSubject(tenant, _u(), "erasure", "contact", _u()))  # not a candidate / worker
    assert await _rows(tenant) == []


async def test_rollback_writes_no_row(tenant: uuid.UUID, no_kick: None) -> None:
    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        async with tenant_session(tenant) as db:
            emit(db, CandidateCreated(tenant, _u()))
            raise _Boom
    assert await _rows(tenant) == []


async def test_savepoint_rollback_drops_its_rows(tenant: uuid.UUID, no_kick: None) -> None:
    kept, dropped, released = _u(), _u(), _u()
    async with tenant_session(tenant) as db:
        emit(db, CandidateCreated(tenant, kept))
        sp = await db.begin_nested()
        emit(db, CandidateCreated(tenant, dropped))
        await sp.rollback()
        async with db.begin_nested():
            emit(db, CandidateCreated(tenant, released))
    assert sorted(str(r.subject_id) for r in await _rows(tenant)) == sorted([str(kept), str(released)])


async def test_actor_and_installation_from_the_request_context(tenant: uuid.UUID, no_kick: None) -> None:
    installation = _u()
    principal = Principal(type="app", id=installation, tenant_id=tenant, app_installation_id=installation)
    token = set_context(RequestContext(tenant_id=tenant, principal=principal))
    try:
        async with tenant_session(tenant) as db:
            emit(db, CandidateUpdated(tenant, _u()))
    finally:
        reset_context(token)
    (row,) = await _rows(tenant)
    assert (row.actor_kind, row.app_installation_id) == ("app", installation)


def test_pii_denylist_validation() -> None:
    class Nested(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: uuid.UUID
        email: str

    class Bad(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: uuid.UUID
        name: str
        contacts: list[Nested] | None = None

    class Loose(BaseModel):
        id: uuid.UUID

    bad = registry.PublicEventSpec(
        type="candidate.leaked", version=1, source=CandidateCreated, subject_type="candidate",
        subject_id=lambda e: e.candidate_id, data_model=Bad, build=lambda e: None, scopes=("candidates:read",),
    )  # fmt: skip
    errors = registry.validate_spec(bad)
    assert any("'name'" in e for e in errors)
    assert any("'contacts.email'" in e for e in errors)
    loose = dataclasses.replace(bad, type="candidate.loose", data_model=Loose)
    assert any("extra='forbid'" in e for e in registry.validate_spec(loose))
    registry.register_public_event(bad)
    try:
        assert any("candidate.leaked" in e and "'name'" in e for e in registry.validate_catalogue())
    finally:
        registry.unregister_public_event("candidate.leaked", 1)
    assert registry.validate_catalogue() == []
    with pytest.raises(ValueError, match="must match"):
        registry.register_public_event(dataclasses.replace(bad, type="Candidate-Leaked"))
    with pytest.raises(ValueError, match="duplicate"):
        registry.register_public_event(registry.spec_for("candidate.created"))
    unknown_scope = dataclasses.replace(bad, scopes=("nope:read",))
    assert any("unknown scope" in e for e in registry.validate_spec(unknown_scope, {"candidates:read"}))


def test_envelope_golden_bytes() -> None:
    row = PublicEventRow(
        id=uuid.UUID("11111111-1111-4111-8111-111111111111"), seq=812345,
        tenant_id=uuid.UUID("22222222-2222-4222-8222-222222222222"), event_type="application.stage_changed",
        event_version=1, visibility="public", subject_type="application",
        subject_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        occurred_at=datetime(2026, 10, 9, 10, 0, 0, 123456, tzinfo=UTC),
        data={"to_stage": "screen", "id": "33333333-3333-4333-8333-333333333333", "from_stage": None, "note_é": "ü"},
        actor_kind="user", app_installation_id=None,
    )  # fmt: skip
    golden = (
        '{"api_version":"v1","data":{"from_stage":null,"id":"33333333-3333-4333-8333-333333333333","note_é":"ü",'
        '"to_stage":"screen"},"id":"11111111-1111-4111-8111-111111111111","livemode":true,'
        '"occurred_at":"2026-10-09T10:00:00Z","sequence":812345,'
        '"subject":{"id":"33333333-3333-4333-8333-333333333333","type":"application"},'
        '"tenant_ref":{"id":"22222222-2222-4222-8222-222222222222","slug":"acme"},'
        '"type":"application.stage_changed","version":1}'
    ).encode()
    assert public_events_service.envelope(row, "acme", True) == golden
    assert b'"livemode":false' in public_events_service.envelope(row, "acme", False)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, ctx: RequestContext, db: AsyncSession, rows: Sequence[PublicEventRow]) -> None:
        self.calls.append([r.event_type for r in rows])


async def test_crash_between_write_and_relay_loses_nothing(tenant: uuid.UUID, no_kick: None) -> None:
    sink = _Recorder()
    public_events_service.register_sink("test_recorder", sink)
    async with tenant_session(tenant) as db:
        emit(db, CandidateCreated(tenant, _u()))
        emit(db, DemandCreated(tenant, _u(), "user"))
    # the process "crashed" after commit: no dispatch ran, the rows are committed and pending
    assert [r.dispatch_status for r in await _rows(tenant)] == ["pending", "pending"]
    assert await public_events_service.sweep_tenant(tenant) == 0  # younger than 30 s: not stalled yet
    assert sink.calls == []
    assert await public_events_service.sweep_tenant(tenant, older_than_seconds=0) == 2
    assert sink.calls == [["candidate.created", "demand.created"]]
    rows = await _rows(tenant)
    assert all(r.dispatch_status == "dispatched" and r.dispatched_at is not None for r in rows)
    assert await public_events_service.dispatch(tenant) == 0  # nothing pending


async def test_failing_sink_leaves_batch_pending(tenant: uuid.UUID, no_kick: None, rls_table: str) -> None:
    async def good(ctx: RequestContext, db: AsyncSession, rows: Sequence[PublicEventRow]) -> None:
        await db.execute(text(f"INSERT INTO {rls_table} (tenant_id, name) VALUES (:t, 'good')"), {"t": ctx.tenant_id})

    async def bad(ctx: RequestContext, db: AsyncSession, rows: Sequence[PublicEventRow]) -> None:
        await db.execute(text(f"INSERT INTO {rls_table} (tenant_id, name) VALUES (:t, 'bad')"), {"t": ctx.tenant_id})
        raise RuntimeError("sink down")

    public_events_service.register_sink("a_good", good)
    public_events_service.register_sink("b_bad", bad)
    async with tenant_session(tenant) as db:
        emit(db, CandidateCreated(tenant, _u()))
    assert await public_events_service.dispatch(tenant) == 0
    assert [r.dispatch_status for r in await _rows(tenant)] == ["pending"]
    async with tenant_session(tenant) as db:
        names = sorted((await db.execute(text(f"SELECT name FROM {rls_table}"))).scalars())
    assert names == ["good"]  # the failing sink's SAVEPOINT was rolled back
    public_events_service.unregister_sink("b_bad")
    assert await public_events_service.dispatch(tenant) == 1  # retried: at least once, nothing lost
    assert [r.dispatch_status for r in await _rows(tenant)] == ["dispatched"]


async def test_sink_filters(tenant: uuid.UUID, no_kick: None) -> None:
    only_demands = _Recorder()
    public_events_service.register_sink("test_demands", only_demands, event_types=frozenset({"demand.created"}))
    async with tenant_session(tenant) as db:
        emit(db, CandidateCreated(tenant, _u()))
        emit(db, DemandCreated(tenant, _u(), "user"))
    assert await public_events_service.dispatch(tenant) == 2
    assert only_demands.calls == [["demand.created"]]


async def test_after_commit_kick_dispatches(tenant: uuid.UUID) -> None:
    sink = _Recorder()
    public_events_service.register_sink("test_kick", sink)
    async with tenant_session(tenant) as db:
        emit(db, CandidateCreated(tenant, _u()))
    assert [r.dispatch_status for r in await _rows(tenant)] == ["dispatched"]  # eager Celery in tests
    assert sink.calls == [["candidate.created"]]


async def test_list_and_get_events(tenant: uuid.UUID, no_kick: None) -> None:
    async with tenant_session(tenant) as db:
        for _ in range(3):
            emit(db, CandidateCreated(tenant, _u()))
        emit(db, DemandCreated(tenant, _u(), "user"))
    ctx = RequestContext(tenant_id=tenant)
    async with tenant_session(tenant) as db:
        page = await public_events_service.list_events(ctx, db, EventQuery(limit=2))
        assert [e.type for e in page.data] == ["candidate.created", "candidate.created"] and page.has_more
        page2 = await public_events_service.list_events(ctx, db, EventQuery(limit=2, cursor=page.next_cursor))
        assert [e.type for e in page2.data] == ["candidate.created", "demand.created"] and not page2.has_more
        assert page2.data[0].sequence > page.data[-1].sequence
        one = await public_events_service.get_event(ctx, db, page.data[0].id)
        assert one.tenant_ref.id == tenant and one.livemode is True and one.api_version == "v1"
        app = Principal(type="app", id=_u(), tenant_id=tenant, oauth_scopes=frozenset({"demands:read"}))
        scoped = await public_events_service.list_events(RequestContext(tenant_id=tenant, principal=app), db, EventQuery())
        assert [e.type for e in scoped.data] == ["demand.created"]

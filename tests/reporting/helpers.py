"""Shared helpers for the Epic 11 reporting tests.

``ROrg`` is a tenant with two business units (A, B), a Tenant Admin, a recruiter, a recruitment manager and
an account manager (all in BU A) plus a second recruiter in BU B. ``Seed`` inserts a hand-made timeline
directly in the tenant session (RLS applies), with explicit timestamps, so every tile value can be computed
by hand.
"""

import dataclasses
import itertools
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from sqlalchemy import select, text, update

from app.core.database import tenant_session
from app.models.activity import Activity
from app.models.candidates import Candidate
from app.models.clients import Client
from app.models.demands import Demand, DemandAssignment, DemandSlaClock
from app.models.identity import BusinessUnit, Tenant, User
from app.models.pipeline import Application, ApplicationStageHistory, PipelineStage, Screening, Submission
from app.models.tasks import Task
from tests.modules.identity.conftest import Member, csrf, join, make_tenant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

API = "/api/v1"
IST = ZoneInfo("Asia/Kolkata")
_seq = itertools.count(1)


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


def ist(y: int, mo: int, d: int, hh: int = 12, mm: int = 0) -> datetime:
    """Return an IST wall-clock time as an aware UTC datetime."""
    return datetime(y, mo, d, hh, mm, tzinfo=IST).astimezone(UTC)


@dataclasses.dataclass
class ROrg:
    tenant: Tenant
    admin: Member
    recruiter: Member
    manager: Member
    account: Member
    recruiter_b: Member
    bu_a: uuid.UUID
    bu_b: uuid.UUID

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    def members(self) -> list[Member]:
        return [self.admin, self.recruiter, self.manager, self.account, self.recruiter_b]

    async def close(self) -> None:
        for m in self.members():
            await m.client.aclose()


async def make_rorg(app: FastAPI, name: str = "Ada Admin") -> ROrg:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name=name)
    recruiter = await join(app, tenant, "recruiter", name="Rita Recruiter")
    manager = await join(app, tenant, "recruitment_manager", name="Manny Manager")
    account = await join(app, tenant, "account_manager", name="Anu Account")
    recruiter_b = await join(app, tenant, "recruiter", name="Bala Recruiter")
    bu_a, bu_b = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(tenant.id) as db:
        db.add_all(
            [
                BusinessUnit(id=bu_a, tenant_id=tenant.id, name="Bengaluru"),
                BusinessUnit(id=bu_b, tenant_id=tenant.id, name="Chennai"),
            ]
        )
        await db.flush()
        for m, bu in (
            (recruiter, bu_a),
            (manager, bu_a),
            (account, bu_a),
            (recruiter_b, bu_b),
            (admin, bu_a),
        ):
            await db.execute(update(User).where(User.id == m.user_id).values(business_unit_id=bu))
    return ROrg(tenant, admin, recruiter, manager, account, recruiter_b, bu_a, bu_b)


def _hid(prefix: str) -> str:
    return f"{prefix}-{next(_seq):06d}"


class Seed:
    """Direct inserts with explicit timestamps (one tenant session; commits on exit)."""

    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.db: AsyncSession
        self._cm: Any = None
        self.stages: dict[str, uuid.UUID] = {}

    async def __aenter__(self) -> "Seed":
        from app.services.pipeline.stages import ensure_default_stages

        self._cm = tenant_session(self.tenant_id)
        self.db = await self._cm.__aenter__()
        await ensure_default_stages(self.db, self.tenant_id)
        rows = await self.db.execute(select(PipelineStage.key, PipelineStage.id))
        self.stages = dict(rows.tuples().all())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._cm.__aexit__(*exc)

    def _add(self, obj: Any) -> Any:
        self.db.add(obj)
        return obj

    async def flush(self) -> None:
        await self.db.flush()

    async def custom_stage(self, key: str, position: int) -> uuid.UUID:
        sid = uuid.uuid4()
        self._add(
            PipelineStage(
                id=sid, tenant_id=self.tenant_id, key=key, label=key.title(), kind="custom", position=position
            )
        )
        await self.db.flush()
        self.stages[key] = sid
        return sid

    async def client(self, owner: uuid.UUID, name: str | None = None) -> uuid.UUID:
        cid = uuid.uuid4()
        self._add(
            Client(id=cid, tenant_id=self.tenant_id, name=name or f"Client {cid.hex[:6]}", owner_id=owner)
        )
        await self.db.flush()
        return cid

    async def demand(
        self,
        client_id: uuid.UUID,
        owner: uuid.UUID,
        *,
        status: str = "open",
        opened_at: datetime | None = None,
        positions: int = 1,
        bu: uuid.UUID | None = None,
        title: str = "Data Engineer",
        bill_rate: Decimal | None = None,
    ) -> uuid.UUID:
        did = uuid.uuid4()
        self._add(
            Demand(
                id=did,
                tenant_id=self.tenant_id,
                req_id=_hid("REQ"),
                title=title,
                client_id=client_id,
                owner_id=owner,
                status=status,
                positions=positions,
                opened_at=opened_at,
                business_unit_id=bu,
                bill_rate=bill_rate,
                currency="INR" if bill_rate is not None else None,
            )
        )
        await self.db.flush()
        return did

    async def assign(self, demand_id: uuid.UUID, user_id: uuid.UUID, at: datetime | None = None) -> None:
        self._add(
            DemandAssignment(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                demand_id=demand_id,
                user_id=user_id,
                is_primary=True,
                assigned_at=at or datetime.now(UTC),
            )
        )
        await self.db.flush()

    async def sla_clock(
        self,
        demand_id: uuid.UUID,
        status: str,
        *,
        milestone: str = "first_submission",
        completed: bool = False,
    ) -> None:
        start = datetime.now(UTC) - timedelta(days=2)
        self._add(
            DemandSlaClock(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                demand_id=demand_id,
                milestone=milestone,
                started_at=start,
                due_at=start + timedelta(days=3),
                completed_at=datetime.now(UTC) if completed else None,
                status=status,
                target_hours=Decimal("72"),
                clock="calendar",
                at_risk_percent=75,
            )
        )
        await self.db.flush()

    async def candidate(
        self, owner: uuid.UUID, created_at: datetime, name: str = "Priya Raman", **extra: Any
    ) -> uuid.UUID:
        cid = uuid.uuid4()
        self._add(
            Candidate(
                id=cid,
                tenant_id=self.tenant_id,
                human_id=_hid("CAN"),
                full_name=name,
                source=extra.pop("source", "manual"),
                owner_id=owner,
                created_at=created_at,
                **extra,
            )
        )
        await self.db.flush()
        return cid

    async def application(
        self,
        candidate_id: uuid.UUID,
        demand_id: uuid.UUID,
        owner: uuid.UUID,
        created_at: datetime,
        *,
        path: list[tuple[str, datetime]] | None = None,
        source: str = "manual",
        archived: bool = False,
    ) -> uuid.UUID:
        """Create at ``applied`` (history row), then move along ``path`` ``[(stage key, at), ...]``."""
        aid = uuid.uuid4()
        steps = [("applied", created_at), *(path or [])]
        final_key, final_at = steps[-1]
        self._add(
            Application(
                id=aid,
                tenant_id=self.tenant_id,
                human_id=_hid("APP"),
                candidate_id=candidate_id,
                demand_id=demand_id,
                stage_id=self.stages[final_key],
                stage_entered_at=final_at,
                source=source,
                owner_id=owner,
                created_at=created_at,
                archived_at=datetime.now(UTC) if archived else None,
            )
        )
        await self.db.flush()
        previous: uuid.UUID | None = None
        for key, at in steps:
            self._add(
                ApplicationStageHistory(
                    id=uuid.uuid4(),
                    tenant_id=self.tenant_id,
                    application_id=aid,
                    from_stage_id=previous,
                    to_stage_id=self.stages[key],
                    actor_kind="user",
                    actor_id=owner,
                    at=at,
                )
            )
            previous = self.stages[key]
        await self.db.flush()
        return aid

    async def submission(
        self,
        application_id: uuid.UUID,
        *,
        sent_at: datetime | None,
        sent_by: uuid.UUID | None,
        status: str = "sent",
        feedback: str = "none",
        bill: str = "1500.00",
        pay: str = "900.00",
    ) -> uuid.UUID:
        app = await self.db.get(Application, application_id)
        assert app is not None
        demand = await self.db.get(Demand, app.demand_id)
        assert demand is not None
        sid = uuid.uuid4()
        self._add(
            Submission(
                id=sid,
                tenant_id=self.tenant_id,
                human_id=_hid("SUB"),
                application_id=application_id,
                demand_id=app.demand_id,
                client_id=demand.client_id,
                candidate_id=app.candidate_id,
                status=status,
                pay_rate=Decimal(pay),
                bill_rate=Decimal(bill),
                currency="INR",
                rate_unit="hour",
                burden_pct=Decimal("18"),
                margin_amount=Decimal(bill) - Decimal(pay),
                margin_pct=Decimal("30.00"),
                resume_kind="original",
                sent_at=sent_at,
                sent_by=sent_by,
                feedback_status=feedback,
                created_by=sent_by,
            )
        )
        await self.db.flush()
        return sid

    async def screening(self, application_id: uuid.UUID, by: uuid.UUID, at: datetime) -> None:
        app = await self.db.get(Application, application_id)
        assert app is not None
        self._add(
            Screening(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                application_id=application_id,
                candidate_id=app.candidate_id,
                kind="recruiter",
                status="completed",
                outcome="proceed",
                completed_by=by,
                completed_at=at,
            )
        )
        await self.db.flush()

    async def interview(
        self,
        application_id: uuid.UUID,
        starts_at: datetime,
        organizer: uuid.UUID,
        *,
        status: str = "scheduled",
        participants: tuple[uuid.UUID, ...] = (),
    ) -> uuid.UUID:
        from app.models.interviews import Interview, InterviewParticipant, InterviewStage

        app = await self.db.get(Application, application_id)
        assert app is not None
        stage_id = (
            await self.db.execute(
                select(InterviewStage.id).where(InterviewStage.demand_id == app.demand_id).limit(1)
            )
        ).scalar_one_or_none()
        if stage_id is None:
            stage_id = uuid.uuid4()
            self._add(
                InterviewStage(
                    id=stage_id,
                    tenant_id=self.tenant_id,
                    demand_id=app.demand_id,
                    position=1,
                    name="Technical",
                    kind="technical",
                )
            )
            await self.db.flush()
        iid = uuid.uuid4()
        self._add(
            Interview(
                id=iid,
                tenant_id=self.tenant_id,
                human_id=_hid("INT"),
                application_id=application_id,
                demand_id=app.demand_id,
                candidate_id=app.candidate_id,
                stage_id=stage_id,
                stage_name="Technical",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
                timezone="Asia/Kolkata",
                mode="video",
                status=status,
                ical_uid=f"{iid}@talent-os.test",
                organizer_user_id=organizer,
            )
        )
        await self.db.flush()
        for user_id in participants:
            self._add(
                InterviewParticipant(
                    id=uuid.uuid4(),
                    tenant_id=self.tenant_id,
                    interview_id=iid,
                    kind="interviewer_internal",
                    user_id=user_id,
                    email=f"{user_id.hex[:10]}@initech.example.com",
                    timezone="Asia/Kolkata",
                )
            )
        await self.db.flush()
        return iid

    async def offer(
        self,
        application_id: uuid.UUID,
        owner: uuid.UUID,
        *,
        status: str = "released",
        released_at: datetime | None = None,
        pay: str = "1000.00",
        bill: str = "1600.00",
    ) -> uuid.UUID:
        from app.models.offers import Offer, OfferVersion

        app = await self.db.get(Application, application_id)
        assert app is not None
        demand = await self.db.get(Demand, app.demand_id)
        assert demand is not None
        oid, vid = uuid.uuid4(), uuid.uuid4()
        self._add(
            Offer(
                id=oid,
                tenant_id=self.tenant_id,
                human_id=_hid("OFR"),
                application_id=application_id,
                demand_id=app.demand_id,
                candidate_id=app.candidate_id,
                client_id=demand.client_id,
                status=status,
                owner_id=owner,
                released_at=released_at,
            )
        )
        await self.db.flush()
        self._add(
            OfferVersion(
                id=vid,
                tenant_id=self.tenant_id,
                offer_id=oid,
                version_no=1,
                title="Data Engineer",
                employment_type="contract",
                pay_rate=Decimal(pay),
                pay_currency="INR",
                pay_rate_unit="hour",
                bill_rate=Decimal(bill),
                bill_currency="INR",
                bill_rate_unit="hour",
                start_date=date(2026, 11, 1),
                end_date=date(2027, 11, 1),
                expiry_date=date(2026, 12, 31),
            )
        )
        await self.db.flush()
        await self.db.execute(
            update(Offer)
            .where(Offer.id == oid)
            .values(current_version_id=vid, released_version_id=vid if released_at else None)
        )
        return oid

    async def activity(self, author: uuid.UUID, kind: str, at: datetime, entity_id: uuid.UUID) -> None:
        self._add(
            Activity(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                entity_type="demand",
                entity_id=entity_id,
                kind=kind,
                body="Logged",
                occurred_at=at,
                author_id=author,
            )
        )
        await self.db.flush()

    async def task(self, owner: uuid.UUID, due_at: datetime | None, *, status: str = "open") -> None:
        self._add(
            Task(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                title="Follow up",
                owner_id=owner,
                created_by=owner,
                due_at=due_at,
                status=status,
            )
        )
        await self.db.flush()

    async def active_days(self, user_id: uuid.UUID, days: list[date]) -> None:
        for d in days:
            await self.db.execute(
                text(
                    "INSERT INTO user_active_days (tenant_id, user_id, day) VALUES (:t, :u, :d) ON CONFLICT DO NOTHING"
                ),
                {"t": self.tenant_id, "u": user_id, "d": d},
            )


async def tile(m: Member, dashboard: str, key: str, **params: Any) -> dict[str, Any]:
    resp = await m.client.get(
        f"{API}/dashboards/{dashboard}/tiles/{key}", params={"refresh": "true", **params}
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def api_path(link: dict[str, Any]) -> str:
    """Frontend drill-down path -> the API list endpoint behind it."""
    path = link["path"]
    if path.startswith("/reports/"):
        return f"{API}/reports/lists/{path.removeprefix('/reports/')}"
    return f"{API}{path}"


async def follow(m: Member, link: dict[str, Any]) -> dict[str, Any]:
    resp = await m.client.get(api_path(link), params=link["query"])
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def gen(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item

"""Manager dashboard tiles (BU-scoped): open / at-risk demands, throughput, SM-1, time to fill, recruiter
activity, stage conversion, source effectiveness, SM-2, SM-6 and SM-3."""

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import DateTime, Interval, and_, cast, distinct, func, literal, select

from app.models.activity import Activity
from app.models.comms import Email
from app.models.demands import Demand
from app.models.pipeline import Application, ApplicationStageHistory, PipelineStage, Screening, Submission
from app.models.reporting import UserActiveDay
from app.services.reporting.common import (
    has_interviews,
    has_offers,
    interviews,
    offers,
    roles,
    user_roles,
    users,
)
from app.services.reporting.filters import REPORT_READ
from app.services.reporting.lists import (
    INTERVIEW_ACTIVE_STATUSES,
    db_sourced_condition,
    demand_condition,
    has_interview_condition,
    stage_ids,
)
from app.services.reporting.metrics import (
    TileContext,
    TileData,
    count_link,
    epoch,
    ids_of,
    link,
    list_query,
    pct,
    rounded,
)

FUNNEL_KEYS = ("applied", "shortlisted", "submitted", "interviewing", "selected", "offered", "hired")
RECRUITER_ROLE = "recruiter"
ACTIVE_DAYS_THRESHOLD = 4


# ================================================================ helpers
def _scope(t: TileContext, demand_col: Any, permission: str) -> list[Any]:
    cond = demand_condition(demand_col, t.principal, permission, t.filters.business_unit_ids, t.client_owner)
    return [] if cond is None else [cond]


def _in_range(t: TileContext, col: Any) -> list[Any]:
    return [col >= t.filters.start, col < t.filters.end]


def hire_ranks(demand_ids: Any = None, until: Any = None) -> Any:
    """Per demand, its hires in order: ``(demand_id, hired_at, rn)`` (an application's first hire).
    ``demand_ids`` (a ``SELECT id``) / ``until`` narrow the hires ranked (the rank of a hire only depends on
    earlier hires of the same demand, so both filters keep ranks exact)."""
    stmt = (
        select(
            Application.demand_id.label("demand_id"), func.min(ApplicationStageHistory.at).label("hired_at")
        )
        .join(Application, Application.id == ApplicationStageHistory.application_id)
        .where(ApplicationStageHistory.to_stage_id.in_(stage_ids("hired")))
        .group_by(ApplicationStageHistory.application_id, Application.demand_id)
    )
    if demand_ids is not None:
        stmt = stmt.where(Application.demand_id.in_(demand_ids))
    if until is not None:
        stmt = stmt.having(func.min(ApplicationStageHistory.at) < until)
    first_hire = stmt.subquery()
    return select(
        first_hire.c.demand_id,
        first_hire.c.hired_at,
        func.row_number()
        .over(partition_by=first_hire.c.demand_id, order_by=first_hire.c.hired_at)
        .label("rn"),
    ).subquery()


def filled_join(ranked: Any) -> Any:
    """Join condition picking the hire that reached the demand's ``positions`` (its fill time)."""
    return and_(ranked.c.demand_id == Demand.id, ranked.c.rn == Demand.positions)


def _recruiters(t: TileContext) -> Any:
    """Active users holding the ``recruiter`` role, narrowed to the dashboard's BUs."""
    stmt = (
        select(users.c.id, users.c.name)
        .join(user_roles, user_roles.c.user_id == users.c.id)
        .join(roles, roles.c.id == user_roles.c.role_id)
        .where(roles.c.key == RECRUITER_ROLE, users.c.is_active.is_(True))
    )
    if t.filters.business_unit_ids:
        stmt = stmt.where(users.c.business_unit_id.in_(t.filters.business_unit_ids))
    else:
        scope = t.principal.scope_of(REPORT_READ)
        if scope is not None and scope.value != "tenant":
            if t.principal.business_unit_id is None:
                stmt = stmt.where(users.c.id == t.principal.id)
            else:
                stmt = stmt.where(users.c.business_unit_id == t.principal.business_unit_id)
    return stmt.distinct()


# ================================================================ KPIs from list queries
async def open_demands(t: TileContext) -> TileData:
    value, lk = await count_link(t, "demands", {"status": ["open"], **t.scope_query()})
    return {"value": value, "link": lk}


async def sla_risk(t: TileContext) -> TileData:
    at_risk, at_risk_link = await count_link(t, "demands", {"sla": "at_risk", **t.scope_query()})
    breached, breached_link = await count_link(t, "demands", {"sla": "breached", **t.scope_query()})
    return {
        "value": at_risk,
        "rows": [
            {"key": "at_risk", "label": "At risk", "value": at_risk},
            {"key": "breached", "label": "Breached", "value": breached},
        ],
        "link": at_risk_link,
        "row_links": [at_risk_link, breached_link],
    }


# ================================================================ throughput (series)
async def throughput(t: TileContext) -> TileData:
    f = t.filters
    g = f.granularity
    tz = f.tz_name
    step = cast(literal(f"1 {g}"), Interval())
    first = func.date_trunc(g, cast(literal(datetime.combine(f.from_date, time.min)), DateTime()))
    last = cast(literal(datetime.combine(f.to_date, time.min)), DateTime())
    buckets = select(func.generate_series(first, last, step).label("b")).subquery()

    def bucket(ts: Any) -> Any:
        return func.date_trunc(g, func.timezone(tz, ts))

    metrics: list[tuple[str, str, Any, str, dict[str, Any]]] = []
    sub = (
        select(bucket(Submission.sent_at).label("b"), func.count().label("n"))
        .where(*_in_range(t, Submission.sent_at), *_scope(t, Submission.demand_id, "submission:read"))
        .group_by("b")
        .subquery()
    )
    metrics.append(("submissions", "Submissions sent", sub, "submissions", {}))
    if has_interviews(t.avail):
        iv = (
            select(bucket(interviews.c.starts_at).label("b"), func.count().label("n"))
            .where(
                *_in_range(t, interviews.c.starts_at),
                interviews.c.status.in_(INTERVIEW_ACTIVE_STATUSES),
                *_scope(t, interviews.c.demand_id, "interview:read"),
            )
            .group_by("b")
            .subquery()
        )
        metrics.append(("interviews", "Interviews scheduled", iv, "interviews", {}))
    if has_offers(t.avail):
        of = (
            select(bucket(offers.c.released_at).label("b"), func.count().label("n"))
            .where(*_in_range(t, offers.c.released_at), *_scope(t, offers.c.demand_id, "offer:read"))
            .group_by("b")
            .subquery()
        )
        metrics.append(("offers", "Offers released", of, "offers", {}))
    hires = (
        select(
            bucket(ApplicationStageHistory.at).label("b"),
            func.count(distinct(ApplicationStageHistory.application_id)).label("n"),
        )
        .join(Application, Application.id == ApplicationStageHistory.application_id)
        .where(
            ApplicationStageHistory.to_stage_id.in_(stage_ids("hired")),
            *_in_range(t, ApplicationStageHistory.at),
            *_scope(t, Application.demand_id, "application:read"),
        )
        .group_by("b")
        .subquery()
    )
    metrics.append(("hires", "Hires", hires, "applications", {"stage": "hired"}))

    cols: list[Any] = [buckets.c.b]
    joined: Any = buckets
    for key, _label, subq, _kind, _extra in metrics:
        cols.append(func.coalesce(subq.c.n, 0).label(key))
        joined = joined.outerjoin(subq, subq.c.b == buckets.c.b)
    rows = (await t.db.execute(select(*cols).select_from(joined).order_by(buckets.c.b))).mappings().all()

    series: list[dict[str, Any]] = []
    for key, label, _subq, kind, extra in metrics:
        points = []
        for r in rows:
            start: date = r["b"].date()
            nxt = _next_bucket(start, g)
            p_from = max(start, f.from_date)
            p_to = min(nxt - timedelta(days=1), f.to_date)
            points.append(
                {
                    "bucket": start.isoformat(),
                    "from": p_from.isoformat(),
                    "to": p_to.isoformat(),
                    "value": int(r[key]),
                }
            )
        query: dict[str, Any] = {**extra, **t.scope_query()}
        if kind != "applications":
            query.update(t.range_query())
        series.append(
            {
                "key": key,
                "label": label,
                "points": points,
                "link": link(f"/reports/{kind}", query),
                "kind": kind,
            }
        )
    return {
        "series": series,
        "value": sum(p["value"] for p in series[0]["points"]),
        "link": series[0]["link"],
        "granularity": g,
    }


def _next_bucket(start: date, granularity: str) -> date:
    if granularity == "day":
        return start + timedelta(days=1)
    if granularity == "week":
        return start + timedelta(days=7)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return date(year, month, 1)


# ================================================================ SM-1 / time to fill
async def time_to_first_submission(t: TileContext) -> TileData:
    first_sent = (
        select(func.min(Submission.sent_at))
        .where(
            Submission.demand_id == Demand.id,
            Submission.sent_at.is_not(None),
            Submission.sent_at >= Demand.opened_at,
        )
        .correlate(Demand)
        .scalar_subquery()
    )
    lq = await list_query(
        t,
        "demands",
        {
            "opened_from": t.filters.from_date.isoformat(),
            "opened_to": t.filters.to_date.isoformat(),
            **t.scope_query(),
        },
    )
    inner = (
        select((epoch(first_sent - Demand.opened_at) / 3600.0).label("hours"))
        .where(Demand.id.in_(ids_of(lq)))
        .subquery()
    )
    median, n, total = (
        await t.db.execute(
            select(
                func.percentile_cont(0.5).within_group(inner.c.hours), func.count(inner.c.hours), func.count()
            ).select_from(inner)
        )
    ).one()
    return {
        "value": rounded(median),
        "unit": "hours",
        "n": int(n),
        "rows": [
            {"key": "with_submission", "label": "Demands with a submission", "value": int(n)},
            {"key": "opened", "label": "Demands opened", "value": int(total)},
        ],
        "link": link(
            "/demands",
            {
                "opened_from": t.filters.from_date.isoformat(),
                "opened_to": t.filters.to_date.isoformat(),
                **t.scope_query(),
            },
        ),
    }


async def time_to_fill(t: TileContext) -> TileData:
    # Only demands that were filled (Filled, or Closed afterwards) can have been filled in range.
    lq = await list_query(t, "demands", {"status": ["filled", "closed"], **t.scope_query()})
    ranked = hire_ranks(ids_of(lq), t.filters.end)
    inner = (
        select((epoch(ranked.c.hired_at - Demand.opened_at) / 86400.0).label("days"))
        .select_from(Demand)
        .join(ranked, filled_join(ranked))
        .where(
            Demand.id.in_(ids_of(lq)),
            Demand.opened_at.is_not(None),
            ranked.c.hired_at >= t.filters.start,
            ranked.c.hired_at < t.filters.end,
        )
        .subquery()
    )
    median, n = (
        await t.db.execute(
            select(func.percentile_cont(0.5).within_group(inner.c.days), func.count()).select_from(inner)
        )
    ).one()
    return {
        "value": rounded(median),
        "unit": "days",
        "n": int(n),
        "link": link("/demands", {"status": ["filled", "closed"], **t.scope_query()}),
    }


# ================================================================ recruiter activity (table)
async def recruiter_activity(t: TileContext) -> TileData:
    people = _recruiters(t).subquery()
    uid = people.c.id

    def count_of(stmt: Any) -> Any:
        return stmt.correlate(people).scalar_subquery()

    cols: list[Any] = [
        uid,
        people.c.name,
        count_of(
            select(func.count())
            .select_from(Application)
            .where(Application.owner_id == uid, *_in_range(t, Application.created_at))
        ).label("applications_added"),
        count_of(
            select(func.count())
            .select_from(Screening)
            .where(
                Screening.completed_by == uid,
                Screening.status == "completed",
                *_in_range(t, Screening.completed_at),
            )
        ).label("screenings_completed"),
        count_of(
            select(func.count())
            .select_from(Submission)
            .where(Submission.sent_by == uid, *_in_range(t, Submission.sent_at))
        ).label("submissions_sent"),
        count_of(
            select(func.count())
            .select_from(Activity)
            .where(
                Activity.author_id == uid,
                Activity.kind.in_(("note", "call", "meeting")),
                *_in_range(t, Activity.occurred_at),
            )
        ).label("notes_logged"),
        count_of(
            select(func.count())
            .select_from(Email)
            .where(Email.sender_user_id == uid, Email.status == "sent", *_in_range(t, Email.sent_at))
        ).label("emails_sent"),
    ]
    if has_interviews(t.avail):
        cols.append(
            count_of(
                select(func.count())
                .select_from(interviews)
                .where(
                    interviews.c.organizer_user_id == uid,
                    interviews.c.status.in_(INTERVIEW_ACTIVE_STATUSES),
                    *_in_range(t, interviews.c.starts_at),
                )
            ).label("interviews_scheduled")
        )
    inner = select(*cols).subquery()
    result = await t.db.execute(
        select(inner).order_by(inner.c.submissions_sent.desc(), inner.c.name, inner.c.id).limit(50)
    )
    rows: list[dict[str, Any]] = []
    for r in result.mappings():
        row = {k: (str(v) if k == "id" else v) for k, v in r.items()}
        row.setdefault("interviews_scheduled", None)
        rows.append(row)
    return {
        "value": len(rows),
        "rows": rows,
        "link": link("/reports/submissions", {**t.range_query(), **t.scope_query()}),
        "row_links": [link("/reports/submissions", {"sent_by": r["id"], **t.range_query()}) for r in rows],
    }


# ================================================================ conversion / sources
async def _stage_labels(t: TileContext) -> dict[str, str]:
    rows = await t.db.execute(select(PipelineStage.key, PipelineStage.label))
    return {str(k): str(v) for k, v in rows.tuples()}


async def stage_conversion(t: TileContext) -> TileData:
    query = {**t.range_query(), **t.scope_query()}
    lq = await list_query(t, "applications", query)
    ids = ids_of(lq).cte("scoped_applications")
    # "Ever reached" = current stage or any stage-history row (same rule as the ``reached`` list filter),
    # computed with one set-based pass instead of a correlated EXISTS per stage.
    visits = (
        select(
            ApplicationStageHistory.application_id.label("app_id"),
            ApplicationStageHistory.to_stage_id.label("stage_id"),
        )
        .join(ids, ids.c.id == ApplicationStageHistory.application_id)
        .union(
            select(Application.id.label("app_id"), Application.stage_id.label("stage_id")).join(
                ids, ids.c.id == Application.id
            )
        )
        .subquery()
    )
    result = await t.db.execute(
        select(PipelineStage.key, func.count(distinct(visits.c.app_id)))
        .join(visits, visits.c.stage_id == PipelineStage.id)
        .where(PipelineStage.key.in_(FUNNEL_KEYS))
        .group_by(PipelineStage.key)
    )
    by_key = {str(k): int(n) for k, n in result.tuples()}
    counts = [by_key.get(k, 0) for k in FUNNEL_KEYS]
    labels = await _stage_labels(t)
    rows: list[dict[str, Any]] = []
    previous: int | None = None
    first: int | None = None
    for key, value in zip(FUNNEL_KEYS, counts, strict=True):
        n = int(value)
        rows.append(
            {
                "key": key,
                "label": labels.get(key, key.capitalize()),
                "value": n,
                "pct_of_previous": None if previous is None else pct(n, previous),
                "pct_of_first": None if first is None else pct(n, first),
            }
        )
        first = n if first is None else first
        previous = n
    return {
        "value": rows[0]["value"],
        "rows": rows,
        "link": link("/reports/applications", query),
        "row_links": [link("/reports/applications", {**query, "reached": r["key"]}) for r in rows],
    }


async def source_effectiveness(t: TileContext) -> TileData:
    query = {**t.range_query(), **t.scope_query()}
    lq = await list_query(t, "applications", query)
    # Set-based flags per application (one pass each) instead of correlated EXISTS per row.
    ids = ids_of(lq).cte("scoped_applications")
    hired_ids = stage_ids("hired")
    sent = (
        select(Submission.application_id.label("app_id"))
        .join(ids, ids.c.id == Submission.application_id)
        .where(Submission.sent_at.is_not(None))
        .distinct()
        .subquery()
    )
    hired = (
        select(ApplicationStageHistory.application_id.label("app_id"))
        .join(ids, ids.c.id == ApplicationStageHistory.application_id)
        .where(ApplicationStageHistory.to_stage_id.in_(hired_ids))
        .union(
            select(Application.id)
            .join(ids, ids.c.id == Application.id)
            .where(Application.stage_id.in_(hired_ids))
        )
        .subquery()
    )
    joined: Any = select(Application.id, Application.source).join(ids, ids.c.id == Application.id).subquery()
    frm: Any = joined.outerjoin(sent, sent.c.app_id == joined.c.id).outerjoin(
        hired, hired.c.app_id == joined.c.id
    )
    cols: list[Any] = [
        joined.c.source,
        func.count().label("applications"),
        func.count(sent.c.app_id).label("submissions"),
        func.count(hired.c.app_id).label("hires"),
    ]
    if has_interviews(t.avail):
        iv = (
            select(interviews.c.application_id.label("app_id"))
            .join(ids, ids.c.id == interviews.c.application_id)
            .where(interviews.c.status.in_(INTERVIEW_ACTIVE_STATUSES))
            .distinct()
            .subquery()
        )
        frm = frm.outerjoin(iv, iv.c.app_id == joined.c.id)
        cols.append(func.count(iv.c.app_id).label("interviews"))
    result = await t.db.execute(
        select(*cols)
        .select_from(frm)
        .group_by(joined.c.source)
        .order_by(func.count().desc(), joined.c.source)
    )
    rows: list[dict[str, Any]] = []
    for r in result.mappings():
        rows.append(
            {
                "source": r["source"],
                "applications": int(r["applications"]),
                "submissions": int(r["submissions"]),
                "interviews": int(r["interviews"]) if "interviews" in r else None,
                "hires": int(r["hires"]),
                "hire_rate": pct(r["hires"], r["applications"]) or 0.0,
            }
        )
    return {
        "value": len(rows),
        "rows": rows,
        "link": link("/reports/applications", query),
        "row_links": [link("/reports/applications", {**query, "source": r["source"]}) for r in rows],
    }


# ================================================================ SM-2 / SM-6
async def db_sourced_submissions(t: TileContext) -> TileData:
    query = {**t.range_query(), **t.scope_query()}
    lq = await list_query(t, "submissions", query)
    part, whole = (
        await t.db.execute(
            select(func.count().filter(db_sourced_condition()), func.count()).where(
                Submission.id.in_(ids_of(lq))
            )
        )
    ).one()
    return {
        "value": pct(part, whole),
        "unit": "%",
        "n": int(whole),
        "rows": [
            {"key": "db_sourced", "label": "Database-sourced", "value": int(part)},
            {"key": "sent", "label": "Submissions sent", "value": int(whole)},
        ],
        "link": link("/reports/submissions", {**query, "db_sourced": True}),
        "row_links": [
            link("/reports/submissions", {**query, "db_sourced": True}),
            link("/reports/submissions", query),
        ],
    }


async def submission_to_interview(t: TileContext) -> TileData:
    query = {**t.range_query(), **t.scope_query()}
    lq = await list_query(t, "submissions", query)
    part, whole = (
        await t.db.execute(
            select(
                func.count().filter(has_interview_condition(Submission.application_id)), func.count()
            ).where(Submission.id.in_(ids_of(lq)))
        )
    ).one()
    return {
        "value": pct(part, whole),
        "unit": "%",
        "n": int(whole),
        "rows": [
            {"key": "with_interview", "label": "With an interview", "value": int(part)},
            {"key": "sent", "label": "Submissions sent", "value": int(whole)},
        ],
        "link": link("/reports/submissions", {**query, "has_interview": True}),
        "row_links": [
            link("/reports/submissions", {**query, "has_interview": True}),
            link("/reports/submissions", query),
        ],
    }


# ================================================================ SM-3
async def active_recruiters(t: TileContext) -> TileData:
    today = t.now.astimezone(t.filters.tz).date()
    people = _recruiters(t).subquery()
    days = (
        select(func.count(distinct(UserActiveDay.day)))
        .where(UserActiveDay.user_id == people.c.id, UserActiveDay.day > today - timedelta(days=7))
        .correlate(people)
        .scalar_subquery()
    )
    active, total = (
        await t.db.execute(
            select(func.count().filter(days >= ACTIVE_DAYS_THRESHOLD), func.count()).select_from(people)
        )
    ).one()
    return {
        "value": pct(active, total),
        "unit": "%",
        "n": int(total),
        "rows": [
            {
                "key": "active",
                "label": f"Active on {ACTIVE_DAYS_THRESHOLD}+ of the last 7 days",
                "value": int(active),
            },
            {"key": "recruiters", "label": "Recruiters", "value": int(total)},
        ],
        "link": link("/admin/users", {"role": RECRUITER_ROLE}),
    }


__all__ = [
    "active_recruiters",
    "db_sourced_submissions",
    "open_demands",
    "recruiter_activity",
    "sla_risk",
    "source_effectiveness",
    "stage_conversion",
    "submission_to_interview",
    "throughput",
    "time_to_fill",
    "time_to_first_submission",
]

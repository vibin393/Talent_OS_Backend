"""Allow-list projections (Epic 9 A9, hard rule): the ONLY place candidate- or public-facing models are built.

Each function copies named fields from plain dicts / rows into a ``Public*`` / ``Portal*`` model (all
``extra="forbid"``). Nothing is ever ``model_validate``d from an internal ORM object or schema, so notes,
scorecards, scores, rates, margins, owners, req ids, internal stage keys, reason codes and hidden client
names cannot reach a candidate or the public.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.settings import get_settings
from app.schemas.careers import (
    PublicApplyQuestion,
    PublicBranding,
    PublicExperience,
    PublicNotice,
    PublicPayRange,
    PublicPosting,
    PublicPostingSummary,
    PublicPrefill,
    PublicSitemapEntry,
)
from app.schemas.portal import (
    PortalApplication,
    PortalApplicationDetail,
    PortalConsent,
    PortalDataExport,
    PortalInterview,
    PortalLocation,
    PortalMoney,
    PortalOffer,
    PortalPrivacyRequest,
    PortalProfile,
    PortalResume,
    PortalScreening,
    PortalScreeningDetail,
    PortalTimelineEntry,
)
from app.services.portal.common import TERMINAL_STAGES, plain_text, render_markdown, stage_label

VALID_THROUGH_DAYS = 60
EMPLOYMENT_TYPES = {
    "permanent": "FULL_TIME",
    "contract": "CONTRACTOR",
    "contract_to_hire": "CONTRACTOR",
    "freelance": "CONTRACTOR",
}
RATE_UNITS = {"hour": "HOUR", "hourly": "HOUR", "day": "DAY", "daily": "DAY", "week": "WEEK", "month": "MONTH",
              "monthly": "MONTH", "year": "YEAR", "annual": "YEAR", "annum": "YEAR"}  # fmt: skip
DSR_STATUS = {
    "received": "open",
    "in_progress": "in_progress",
    "completed": "completed",
    "refused": "rejected",
}


def _money(value: Any) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


# ================================================================ public: branding
def asset_url(slug: str, which: str) -> str:
    return f"/api/v1/public/careers/{slug}/{which}"


def sign_in_url(slug: str) -> str:
    return f"/portal/sign-in?tenant={slug}"


def branding(site: Any) -> PublicBranding:
    return PublicBranding(
        slug=site.slug,
        display_name=site.display_name,
        logo_url=asset_url(site.slug, "logo") if site.logo_file_id else None,
        header_url=asset_url(site.slug, "header") if site.header_file_id else None,
        brand_color=site.brand_color,
        about_html=site.about_html or None,
        privacy_contact_email=site.privacy_contact_email,
        sign_in_url=sign_in_url(site.slug),
    )


# ================================================================ public: postings
def _location(row: dict[str, Any]) -> tuple[str | None, str | None]:
    if not row["show_location"]:
        return None, None
    return row.get("location"), row.get("country")


def _client(row: dict[str, Any]) -> str | None:
    return str(row["client_name"]) if row["show_client_name"] and row.get("client_name") else None


def posting_summary(row: dict[str, Any]) -> PublicPostingSummary:
    location, country = _location(row)
    return PublicPostingSummary(
        id=row["posting_id"],
        slug=row["slug"] or "job",
        title=row["title"],
        location=location,
        country=country,
        work_mode=row.get("work_mode"),
        employment_type=row.get("employment_type"),
        client_name=_client(row),
        published_at=row.get("published_at"),
    )


def _pay_range(row: dict[str, Any]) -> PublicPayRange | None:
    if not row["show_rate"] or (row.get("pay_rate_min") is None and row.get("pay_rate_max") is None):
        return None
    return PublicPayRange(
        min=_money(row.get("pay_rate_min")),
        max=_money(row.get("pay_rate_max")),
        currency=row.get("currency"),
        unit=row.get("rate_unit"),
    )


def job_posting_jsonld(row: dict[str, Any], site: Any, description_html: str) -> dict[str, Any]:
    """schema.org ``JobPosting``. The hiring organisation is the tenant unless the client name is shown."""
    published: datetime | None = row.get("published_at")
    base = get_settings().frontend_url.rstrip("/")
    client = _client(row)
    org: dict[str, Any] = {"@type": "Organization", "name": client or site.display_name}
    if client is None and site.logo_file_id:
        org["logo"] = f"{base}{asset_url(site.slug, 'logo')}"
    data: dict[str, Any] = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": row["title"],
        "description": description_html or plain_text(description_html) or row["title"],
        "identifier": {"@type": "PropertyValue", "name": site.display_name, "value": str(row["posting_id"])},
        "directApply": True,
        "hiringOrganization": org,
    }
    if published is not None:
        data["datePosted"] = published.date().isoformat()
        data["validThrough"] = (published + timedelta(days=VALID_THROUGH_DAYS)).isoformat()
    employment = EMPLOYMENT_TYPES.get(row.get("employment_type") or "")
    if employment:
        data["employmentType"] = employment
    location, country = _location(row)
    if row.get("work_mode") == "remote":
        data["jobLocationType"] = "TELECOMMUTE"
        data["applicantLocationRequirements"] = {"@type": "Country", "name": country or "IN"}
    elif location or country:
        address: dict[str, Any] = {"@type": "PostalAddress"}
        if location:
            address["addressLocality"] = location
        if country:
            address["addressCountry"] = country
        data["jobLocation"] = {"@type": "Place", "address": address}
    pay = _pay_range(row)
    if pay is not None:
        value: dict[str, Any] = {
            "@type": "QuantitativeValue",
            "unitText": RATE_UNITS.get((pay.unit or "").lower(), "HOUR"),
        }
        if pay.min is not None:
            value["minValue"] = float(pay.min)
        if pay.max is not None:
            value["maxValue"] = float(pay.max)
        data["baseSalary"] = {"@type": "MonetaryAmount", "currency": pay.currency or "INR", "value": value}
    return data


def posting_detail(row: dict[str, Any], site: Any) -> PublicPosting:
    description_html = render_markdown(row.get("content") or "")
    location, country = _location(row)
    return PublicPosting(
        id=row["posting_id"],
        slug=row["slug"] or "job",
        title=row["title"],
        description_html=description_html,
        work_mode=row.get("work_mode"),
        employment_type=row.get("employment_type"),
        experience=PublicExperience(
            min=_num(row.get("min_experience_years")), max=_num(row.get("max_experience_years"))
        ),
        skills=[str(s) for s in row.get("skills") or []],
        published_at=row.get("published_at"),
        location=location,
        country=country,
        client_name=_client(row),
        pay_range=_pay_range(row),
        jsonld=job_posting_jsonld(row, site, description_html),
    )


def sitemap_entry(row: dict[str, Any]) -> PublicSitemapEntry:
    return PublicSitemapEntry(
        posting_id=row["posting_id"], slug=row["slug"] or "job", lastmod=row.get("lastmod")
    )


def notice(row: Any) -> PublicNotice:
    return PublicNotice(
        id=row.id,
        purpose=row.purpose,
        version=row.version,
        title=row.title,
        body=row.body_md,
        body_html=render_markdown(row.body_md),
    )


def question(q: dict[str, Any]) -> PublicApplyQuestion:
    return PublicApplyQuestion(
        key=str(q["key"]),
        label=str(q["label"]),
        type=str(q["type"]),
        options=[str(o) for o in q["options"]] if q.get("options") else None,
        required=bool(q.get("required")),
    )


def prefill(data: dict[str, Any] | None) -> PublicPrefill:
    d = data or {}
    return PublicPrefill(
        full_name=d.get("full_name"),
        email=d.get("email"),
        phone=d.get("phone"),
        location=d.get("location"),
        current_title=d.get("current_title"),
        current_employer=d.get("current_employer"),
        total_experience_months=d.get("total_experience_months"),
        skills=[str(s) for s in d.get("skills") or []],
        links=[str(u) for u in d.get("links") or []],
    )


# ================================================================ portal: profile / resumes
def profile(d: dict[str, Any]) -> PortalProfile:
    loc = d.get("location") if isinstance(d.get("location"), dict) else None
    comp = None
    if d.get("expected_ctc_amount") is not None or d.get("expected_ctc_currency"):
        comp = PortalMoney(
            amount=_money(d.get("expected_ctc_amount")),
            currency=d.get("expected_ctc_currency"),
            unit=d.get("expected_ctc_unit"),
        )
    return PortalProfile(
        full_name=d["full_name"],
        emails=list(d.get("emails") or []),
        phones=list(d.get("phones") or []),
        location=PortalLocation(city=loc.get("city"), state=loc.get("state"), country=loc.get("country"))
        if loc
        else None,
        links=list(d.get("links") or []),
        headline=d.get("current_title"),
        notice_period_days=d.get("notice_period_days"),
        expected_compensation=comp,
        willing_to_relocate=d.get("willing_to_relocate"),
        work_authorization=d.get("work_authorization"),
    )


def resume(d: dict[str, Any]) -> PortalResume:
    return PortalResume(
        id=d["id"],
        version=d.get("version"),
        file_name=d["file_name"],
        size_bytes=int(d["size_bytes"]),
        content_type=d["content_type"],
        uploaded_at=d["uploaded_at"],
        download_url=f"/api/v1/portal/resumes/{d['id']}/download",
    )


# ================================================================ portal: applications
def application(
    app: dict[str, Any], info: dict[str, Any], labels: dict[str, Any] | None
) -> PortalApplication:
    """``info``: ``{title, client_name, show_client_name}`` of the application's demand / posting."""
    closed = app["stage_key"] in TERMINAL_STAGES
    return PortalApplication(
        id=app["id"],
        title=info.get("title") or "Job application",
        client_name=str(info["client_name"])
        if info.get("show_client_name") and info.get("client_name")
        else None,
        stage_label=stage_label(app["stage_key"], app["stage_kind"], labels),
        status="closed" if closed else "active",
        applied_at=app["applied_at"],
        updated_at=app["updated_at"],
        can_withdraw=not closed,
    )


def timeline(
    history: list[dict[str, Any]], applied_at: datetime, labels: dict[str, Any] | None
) -> list[PortalTimelineEntry]:
    """Collapse the stage history by candidate label; consecutive duplicates are removed."""
    entries: list[PortalTimelineEntry] = []
    for h in history or [{"key": "applied", "kind": "anchored", "at": applied_at}]:
        label = stage_label(h["key"], h["kind"], labels)
        if entries and entries[-1].label == label:
            continue
        entries.append(PortalTimelineEntry(label=label, at=h["at"]))
    return entries


def application_detail(
    app: dict[str, Any], info: dict[str, Any], labels: dict[str, Any] | None, history: list[dict[str, Any]]
) -> PortalApplicationDetail:
    base = application(app, info, labels)
    return PortalApplicationDetail(**base.model_dump(), timeline=timeline(history, app["applied_at"], labels))


# ================================================================ portal: interviews / offers
def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def interview(obj: Any, title: str | None = None) -> PortalInterview:
    mode = _get(obj, "mode")
    return PortalInterview(
        id=_get(obj, "id"),
        title=title or _get(obj, "title") or _get(obj, "demand_title") or "Interview",
        round_name=_get(obj, "round_name") or _get(obj, "stage_name"),
        starts_at=_get(obj, "starts_at"),
        ends_at=_get(obj, "ends_at"),
        timezone=_get(obj, "timezone"),
        mode=mode,
        location=_get(obj, "location") if mode == "in_person" else None,
        join_url=_get(obj, "join_url") or _get(obj, "meeting_url"),
        status=str(_get(obj, "status") or "scheduled"),
    )


def offer(obj: Any) -> PortalOffer:
    comp = _get(obj, "pay_rate") or _get(obj, "compensation")
    money = None
    if comp is not None:
        money = PortalMoney(
            amount=_money(_get(comp, "amount")),
            currency=_get(comp, "currency"),
            unit=_get(obj, "pay_rate_unit") or _get(comp, "unit"),
        )
    start = _get(obj, "start_date")
    return PortalOffer(
        id=_get(obj, "id"),
        title=_get(obj, "title") or _get(obj, "job_title"),
        status=str(_get(obj, "status") or ""),
        start_date=start.isoformat() if hasattr(start, "isoformat") else (str(start) if start else None),
        compensation=money,
        expires_at=_get(obj, "expires_at"),
    )


# ================================================================ portal: AI screenings
def screening(row: Any, title: str) -> PortalScreening:
    return PortalScreening(id=row.id, title=title, status=row.status, sent_at=row.invite_sent_at)


def screening_detail(
    row: Any, title: str, consent_required: bool, notice_row: Any | None
) -> PortalScreeningDetail:
    return PortalScreeningDetail(
        id=row.id,
        title=title,
        status=row.status,
        sent_at=row.invite_sent_at,
        consent_required=consent_required,
        notice=notice(notice_row) if notice_row is not None else None,
        questions=[question(q) for q in row.questions or []],
    )


# ================================================================ portal: privacy
def consent(row: Any, client_name: str | None, notice_version: int | None) -> PortalConsent:
    return PortalConsent(
        id=row.id,
        purpose=row.purpose,
        client_name=client_name if row.purpose == "client_submission" else None,
        status=row.status,
        granted_at=row.granted_at,
        withdrawn_at=row.withdrawn_at,
        notice_version=notice_version,
    )


def data_export(row: Any) -> PortalDataExport:
    return PortalDataExport(
        id=row.id,
        status=row.status,
        requested_at=row.requested_at,
        completed_at=row.completed_at,
        expires_at=row.expires_at,
        download_url=f"/api/v1/portal/data-exports/{row.id}/download" if row.status == "ready" else None,
    )


def privacy_request(row: Any) -> PortalPrivacyRequest:
    return PortalPrivacyRequest(
        id=row.id,
        kind=row.kind,
        status=DSR_STATUS.get(row.status, "open"),
        received_at=row.received_at,
        due_at=row.due_at,
        completed_at=row.completed_at,
    )


def answers(questions: list[dict[str, Any]], values: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the candidate's own answers for the export: ``[{question, answer}]`` (labels, never mapping
    targets)."""
    labels = {str(q.get("key")): str(q.get("label")) for q in questions or []}
    return [{"question": labels.get(k, k), "answer": v} for k, v in (values or {}).items()]

"""Offer versions (story 8.1, FR-43 / FR-44): terms, validation, margin, copy-on-revise and the PDF letter.

* A version is editable only while ``frozen=false``; it freezes on submit (unfrozen again when the approver
  returns it) and never unfreezes once approved. Versions that inherit an approval (``not_required``) are
  frozen from the start.
* ``APPROVAL_RELEVANT`` fields changed on an approved / released offer require a new approval (FR-44).
* ``ensure_letter`` renders the letter PDF idempotently (skips when the input hash matches; a frozen version
  with a letter is never re-rendered) and stores it through ``files_service`` (``core.storage`` + scanning).
"""

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.offers import Offer, OfferSettings, OfferVersion
from app.schemas.offers import Money, OfferTermsIn, OfferVersionBrief, OfferVersionOut
from app.services.offers import letter_pdf, templates
from app.services.offers.common import (
    candidate_facts,
    client_name,
    money,
    q2,
    settings_row,
    tenant_facts,
    tenant_today,
)

APPROVAL_RELEVANT: tuple[str, ...] = (
    "pay_rate",
    "pay_currency",
    "pay_rate_unit",
    "bonus_amount",
    "bill_rate",
    "bill_rate_unit",
    "start_date",
    "employment_type",
)
OTHER_FIELDS: tuple[str, ...] = (
    "benefits_summary",
    "location",
    "work_mode",
    "end_date",
    "expiry_date",
    "title",
    "template_id",
    "bill_currency",
    "bonus_currency",
)
TERM_COLUMNS: tuple[str, ...] = (*APPROVAL_RELEVANT, *OTHER_FIELDS)
CONTRACT_TYPES = ("contract", "contract_to_hire")


# ================================================================ terms
def changes_from(data: OfferTermsIn) -> dict[str, Any]:
    """Map the request (money objects) to version columns; only fields the caller sent."""
    sent = data.model_fields_set
    out: dict[str, Any] = {}
    for key in (
        "title",
        "employment_type",
        "pay_rate_unit",
        "bill_rate_unit",
        "benefits_summary",
        "start_date",
        "end_date",
        "location",
        "work_mode",
        "expiry_date",
        "template_id",
    ):
        if key in sent:
            value = getattr(data, key)
            out[key] = value.strip() if isinstance(value, str) else value
    if "pay_rate" in sent:
        if data.pay_rate is None:
            raise ValidationFailed("The pay rate is required", code="pay_rate_required")
        out["pay_rate"] = q2(data.pay_rate.amount)
        out["pay_currency"] = data.pay_rate.currency
    if "bill_rate" in sent:
        out["bill_rate"] = q2(data.bill_rate.amount) if data.bill_rate else None
        out["bill_currency"] = data.bill_rate.currency if data.bill_rate else None
    if "bonus" in sent:
        out["bonus_amount"] = q2(data.bonus.amount) if data.bonus else None
        out["bonus_currency"] = data.bonus.currency if data.bonus else None
    for required in ("title", "employment_type", "pay_rate_unit", "start_date", "expiry_date"):
        if required in out and out[required] in (None, ""):
            raise ValidationFailed(
                f"{required} is required", code="field_required", details={"field": required}
            )
    return out


def _norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return q2(value)
    return value


def changed_fields(version: OfferVersion, changes: dict[str, Any]) -> set[str]:
    return {k for k, v in changes.items() if _norm(getattr(version, k)) != _norm(v)}


def apply(version: OfferVersion, changes: dict[str, Any]) -> None:
    for key, value in changes.items():
        setattr(version, key, value)
    if version.bill_rate is not None and not version.bill_rate_unit:
        version.bill_rate_unit = version.pay_rate_unit


def validate(version: OfferVersion, tz_name: str | None) -> None:
    """422: contract end date, currencies, positive amounts, expiry in the past (tenant timezone)."""
    if version.employment_type in CONTRACT_TYPES:
        if version.end_date is None:
            raise ValidationFailed(
                "An end date is required for contract offers",
                code="end_date_required",
                details={"field": "end_date"},
            )
    if version.end_date is not None and version.end_date <= version.start_date:
        raise ValidationFailed(
            "The end date must be after the start date",
            code="end_date_before_start",
            details={"field": "end_date"},
        )
    if version.pay_rate is None or Decimal(version.pay_rate) <= 0:
        raise ValidationFailed(
            "The pay rate must be greater than zero", code="invalid_amount", details={"field": "pay_rate"}
        )
    if version.bill_rate is not None:
        if Decimal(version.bill_rate) <= 0:
            raise ValidationFailed(
                "The bill rate must be greater than zero",
                code="invalid_amount",
                details={"field": "bill_rate"},
            )
        if version.bill_currency != version.pay_currency:
            raise ValidationFailed(
                "Pay rate and bill rate must use one currency",
                code="currency_mismatch",
                details={"pay": version.pay_currency, "bill": version.bill_currency},
            )
    if version.bonus_amount is not None and Decimal(version.bonus_amount) <= 0:
        raise ValidationFailed(
            "The bonus must be greater than zero", code="invalid_amount", details={"field": "bonus"}
        )
    check_expiry(version.expiry_date, tz_name)


def check_expiry(expiry_date: date, tz_name: str | None) -> None:
    if expiry_date < tenant_today(tz_name):
        raise ValidationFailed(
            "The expiry date is in the past", code="expiry_in_past", details={"field": "expiry_date"}
        )


# ================================================================ margin
async def compute_margin(ctx: RequestContext, db: AsyncSession, version: OfferVersion) -> None:
    """Margin via the Epic 6 pure function (burden from ``pipeline_settings``); None without a bill rate."""
    from app.services import pipeline_service

    if version.bill_rate is None:
        version.burden_pct = None
        version.margin_amount = None
        version.margin_pct = None
        return
    settings = await pipeline_service.settings.get_settings(ctx, db)
    burden = Decimal(str(settings.burden_pct.get(version.employment_type, 0)))
    m = pipeline_service.margin.compute(
        pay=Decimal(version.pay_rate),
        bill=Decimal(version.bill_rate),
        pay_currency=version.pay_currency,
        bill_currency=version.bill_currency or version.pay_currency,
        rate_unit=version.bill_rate_unit or version.pay_rate_unit,
        burden_pct=burden,
        min_pct=Decimal(settings.min_margin_pct),
        pay_unit=version.pay_rate_unit,
        bill_unit=version.bill_rate_unit or version.pay_rate_unit,
    )
    version.burden_pct = burden
    version.margin_amount = m.amount
    version.margin_pct = m.pct


async def min_margin_pct(ctx: RequestContext, db: AsyncSession) -> Decimal:
    from app.services import pipeline_service

    return Decimal((await pipeline_service.settings.get_settings(ctx, db)).min_margin_pct)


def copy_version(src: OfferVersion, version_no: int, created_by: uuid.UUID | None) -> OfferVersion:
    data = {c: getattr(src, c) for c in TERM_COLUMNS}
    return OfferVersion(
        id=uuid.uuid4(),
        tenant_id=src.tenant_id,
        offer_id=src.offer_id,
        version_no=version_no,
        burden_pct=src.burden_pct,
        margin_amount=src.margin_amount,
        margin_pct=src.margin_pct,
        template_version=None,
        created_by=created_by,
        frozen=False,
        approval_state="pending",
        **data,
    )


# ================================================================ reads
async def current(db: AsyncSession, offer: Offer) -> OfferVersion:
    version = await db.get(OfferVersion, offer.current_version_id) if offer.current_version_id else None
    if version is None:  # pragma: no cover - every offer has a version
        msg = f"offer {offer.id} has no current version"
        raise RuntimeError(msg)
    return version


async def by_no(db: AsyncSession, offer_id: uuid.UUID, version_no: int) -> OfferVersion | None:
    return (
        await db.execute(
            select(OfferVersion).where(
                OfferVersion.offer_id == offer_id, OfferVersion.version_no == version_no
            )
        )
    ).scalar_one_or_none()


async def all_for(db: AsyncSession, offer_id: uuid.UUID) -> list[OfferVersion]:
    return list(
        (
            await db.execute(
                select(OfferVersion)
                .where(OfferVersion.offer_id == offer_id)
                .order_by(OfferVersion.version_no)
            )
        ).scalars()
    )


async def next_no(db: AsyncSession, offer_id: uuid.UUID) -> int:
    rows = await all_for(db, offer_id)
    return (max((v.version_no for v in rows), default=0)) + 1


def version_out(v: OfferVersion, *, show_money: bool, min_pct: Decimal | None) -> OfferVersionOut:
    bill = money(v.bill_rate, v.bill_currency) if show_money else None
    margin = money(v.margin_amount, v.pay_currency) if show_money else None
    pct = f"{q2(Decimal(v.margin_pct)):.2f}" if show_money and v.margin_pct is not None else None
    below = (
        (Decimal(v.margin_pct) < min_pct)
        if show_money and v.margin_pct is not None and min_pct is not None
        else None
    )
    pay = money(v.pay_rate, v.pay_currency)
    assert pay is not None
    bonus = money(v.bonus_amount, v.bonus_currency)
    return OfferVersionOut(
        version_no=v.version_no,
        title=v.title,
        employment_type=v.employment_type,
        pay_rate=Money(**pay),
        pay_rate_unit=v.pay_rate_unit,
        bill_rate=Money(**bill) if bill else None,
        bill_rate_unit=v.bill_rate_unit if show_money else None,
        burden_pct=f"{q2(Decimal(v.burden_pct)):.2f}" if show_money and v.burden_pct is not None else None,
        margin_amount=Money(**margin) if margin else None,
        margin_pct=pct,
        min_margin_pct=f"{q2(min_pct):.2f}" if show_money and min_pct is not None else None,
        below_min_margin=below,
        bonus=Money(**bonus) if bonus else None,
        benefits_summary=v.benefits_summary,
        start_date=v.start_date,
        end_date=v.end_date,
        location=v.location,
        work_mode=v.work_mode,
        expiry_date=v.expiry_date,
        template_id=v.template_id,
        template_version=v.template_version,
        letter_file_id=v.letter_file_id,
        letter_sha256=v.letter_sha256,
        letter_ready=v.letter_file_id is not None,
        approval_state=v.approval_state,
        approved_at=v.approved_at,
        frozen=v.frozen,
        created_by=v.created_by,
        created_at=v.created_at,
    )


def brief(v: OfferVersion, released_version_id: uuid.UUID | None) -> OfferVersionBrief:
    return OfferVersionBrief(
        version_no=v.version_no,
        approval_state=v.approval_state,
        frozen=v.frozen,
        created_at=v.created_at,
        released=v.id == released_version_id,
    )


# ================================================================ letter
async def letter_markdown(
    db: AsyncSession, offer: Offer, version: OfferVersion, settings: OfferSettings | None = None
) -> tuple[str, dict[str, Any], int | None]:
    """Return ``(rendered markdown, tenant facts, template version)`` for ``version``."""
    from app.services.demands import pipeline_port as demands_port

    row = settings or await settings_row(db)
    tenant = await tenant_facts(db, offer.tenant_id)
    template = None
    if version.template_id is not None:
        template = await templates.get_template(db, version.template_id)
    if template is None:
        template = await templates.default_template_for(db, version.employment_type)
    body = (
        template.body_md if template is not None else templates.DEFAULT_TEMPLATES[version.employment_type][1]
    )
    demand = await demands_port.get_row(db, offer.demand_id)
    context = templates.build_context(
        candidate=await candidate_facts(db, offer.candidate_id),
        version=version,
        offer_ref=offer.human_id,
        demand_title=demand.title if demand else None,
        client=await client_name(db, offer.client_id),
        tenant_name=tenant["name"],
        signatory_name=row.signatory_name,
        signatory_title=row.signatory_title,
        today=tenant_today(tenant["timezone"]),
    )
    return templates.render_body(body, context), tenant, (template.version if template is not None else None)


def input_hash(markdown: str, tenant_name: str, offer_ref: str, version_no: int) -> str:
    raw = "\x1f".join((markdown, tenant_name, offer_ref, str(version_no)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def render_pdf(
    db: AsyncSession, offer: Offer, version: OfferVersion
) -> tuple[bytes, str, str, int | None]:
    """``(pdf, markdown, input hash, template version)`` without persisting anything (preview)."""
    markdown, tenant, template_version = await letter_markdown(db, offer, version)
    pdf = letter_pdf.render(
        markdown, tenant_name=tenant["name"], offer_ref=offer.human_id, version_no=version.version_no
    )
    return (
        pdf,
        markdown,
        input_hash(markdown, tenant["name"], offer.human_id, version.version_no),
        template_version,
    )


async def ensure_letter(ctx: RequestContext, db: AsyncSession, offer: Offer, version: OfferVersion) -> bool:
    """Render and store the version's letter unless it is up to date. Returns True when a PDF was stored."""
    from app.services import files_service

    if version.frozen and version.letter_file_id is not None:
        return False
    if version.frozen and version.rendered_body:
        # Frozen before its letter existed (inherited approval): render the stored body, never re-merge.
        tenant = await tenant_facts(db, offer.tenant_id)
        markdown, template_version = version.rendered_body, version.template_version
        digest = input_hash(markdown, tenant["name"], offer.human_id, version.version_no)
        pdf = letter_pdf.render(
            markdown, tenant_name=tenant["name"], offer_ref=offer.human_id, version_no=version.version_no
        )
    else:
        markdown, tenant, template_version = await letter_markdown(db, offer, version)
        digest = input_hash(markdown, tenant["name"], offer.human_id, version.version_no)
        if version.letter_input_hash == digest and version.letter_file_id is not None:
            return False
        pdf = letter_pdf.render(
            markdown, tenant_name=tenant["name"], offer_ref=offer.human_id, version_no=version.version_no
        )
    task_ctx = ctx if ctx.tenant_id is not None else RequestContext(tenant_id=offer.tenant_id)
    stored = await files_service.store_upload(
        task_ctx, db, pdf, f"{offer.human_id}-v{version.version_no}.pdf", {"pdf"}, "offer", offer.id
    )
    version.rendered_body = markdown
    version.template_version = template_version
    version.letter_file_id = stored.id
    version.letter_sha256 = stored.sha256
    version.letter_input_hash = digest
    await db.flush()
    logger.info("Offer {} v{} letter stored ({})", offer.id, version.version_no, stored.id)
    return True

"""Offer letter templates and merge fields (story 8.1).

Templates are Markdown with Jinja2 merge fields rendered in a ``SandboxedEnvironment`` with ``StrictUndefined``
and autoescaping. Only the whitelisted ``MERGE_FIELDS`` may be referenced: bill rate, margin and burden are
never available to a letter. Saving a template statically checks every referenced field (422
``unknown_merge_field`` with ``details.field``) and renders it against a sample context.

``ensure_default_templates`` seeds one default template per employment type (tenant provisioning, lazily on
first use; the migration seeds existing tenants).
"""

import uuid
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError, nodes
from jinja2.sandbox import SandboxedEnvironment, SecurityError  # type: ignore[attr-defined]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.offers import EMPLOYMENT_TYPES, OfferTemplate
from app.schemas.offers import MergeFieldOut, OfferTemplateIn, OfferTemplateOut, OfferTemplateUpdate
from app.services.offers.common import (
    EMPLOYMENT_LABELS,
    UNIT_LABELS,
    WORK_MODE_LABELS,
    fmt_date,
    fmt_money,
    now,
    tenant_of,
)

# key -> (label, sample value)
MERGE_FIELDS: dict[str, tuple[str, str]] = {
    "candidate.full_name": ("Candidate full name", "Asha Rao"),
    "candidate.first_name": ("Candidate first name", "Asha"),
    "offer.title": ("Job title", "Senior Data Engineer"),
    "offer.employment_type": ("Employment type", "Contract"),
    "offer.pay_rate": ("Pay rate", "INR 1,200.00"),
    "offer.pay_rate_unit": ("Pay rate unit", "per hour"),
    "offer.bonus": ("Bonus", "INR 50,000.00"),
    "offer.benefits_summary": ("Benefits summary", "Health insurance for you and your family."),
    "offer.start_date": ("Start date", "01 Nov 2026"),
    "offer.end_date": ("End date", "30 Apr 2027"),
    "offer.location": ("Location", "Chennai"),
    "offer.work_mode": ("Work mode", "Hybrid"),
    "offer.expiry_date": ("Offer expiry date", "15 Oct 2026"),
    "offer.reference": ("Offer reference", "OFR-000123 v1"),
    "demand.title": ("Demand title", "Senior Data Engineer"),
    "client.name": ("Client name", "Globex Corporation"),
    "tenant.name": ("Company name", "Initech Talent"),
    "signatory.name": ("Signatory name", "Meera Iyer"),
    "signatory.title": ("Signatory title", "Head of Delivery"),
    "today": ("Today's date", "30 Sep 2026"),
}

_ENV = SandboxedEnvironment(autoescape=True, undefined=StrictUndefined, keep_trailing_newline=True)


def merge_fields() -> list[MergeFieldOut]:
    return [
        MergeFieldOut(key=k, label=label, example=example) for k, (label, example) in MERGE_FIELDS.items()
    ]


# ================================================================ static field check
def _dotted(node: nodes.Node) -> str | None:
    if isinstance(node, nodes.Name):
        return node.name
    if isinstance(node, nodes.Getattr):
        base = _dotted(node.node)
        return f"{base}.{node.attr}" if base else None
    if (
        isinstance(node, nodes.Getitem)
        and isinstance(node.arg, nodes.Const)
        and isinstance(node.arg.value, str)
    ):
        base = _dotted(node.node)
        return f"{base}.{node.arg.value}" if base else None
    return None


def _assigned_names(tree: nodes.Template) -> set[str]:
    names: set[str] = set()
    for n in tree.find_all((nodes.For, nodes.Assign, nodes.AssignBlock)):
        target = getattr(n, "target", None)
        if target is not None:
            names |= {t.name for t in target.find_all(nodes.Name)} | (
                {target.name} if isinstance(target, nodes.Name) else set()
            )
    return names


def referenced_fields(body: str) -> list[str]:
    """Every data reference of the template, outermost dotted form (``offer.pay_rate``)."""
    try:
        tree = _ENV.parse(body)
    except TemplateSyntaxError as exc:
        raise ValidationFailed(
            "The template has a syntax error", code="template_syntax_error", details={"line": exc.lineno}
        ) from exc
    local = _assigned_names(tree)
    found: list[str] = []
    inner: set[int] = set()
    for node in tree.find_all((nodes.Getattr, nodes.Getitem)):
        inner.add(id(node.node))  # type: ignore[attr-defined]
    for node in tree.find_all((nodes.Getattr, nodes.Getitem, nodes.Name)):
        if id(node) in inner:
            continue
        if isinstance(node, nodes.Name) and node.ctx != "load":
            continue
        dotted = _dotted(node)
        if dotted is None:
            # e.g. ``foo()[0]`` -- only plain field references are allowed.
            raise ValidationFailed(
                "Only merge fields can be used in a template",
                code="unknown_merge_field",
                details={"field": None},
            )
        if dotted.split(".", 1)[0] in local:
            continue
        found.append(dotted)
    return found


def check_fields(body: str) -> None:
    for field in referenced_fields(body):
        if field not in MERGE_FIELDS:
            raise ValidationFailed(
                f"Unknown merge field: {field}", code="unknown_merge_field", details={"field": field}
            )


# ================================================================ rendering
def _nest(flat: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in flat.items():
        head, _, tail = key.partition(".")
        if tail:
            out.setdefault(head, {})[tail] = value
        else:
            out[head] = value
    return out


def sample_context() -> dict[str, Any]:
    return _nest({k: example for k, (_, example) in MERGE_FIELDS.items()})


def build_context(
    *,
    candidate: dict[str, Any],
    version: Any,
    offer_ref: str,
    demand_title: str | None,
    client: str | None,
    tenant_name: str,
    signatory_name: str | None,
    signatory_title: str | None,
    today: date,
) -> dict[str, Any]:
    """Return the whitelisted context of one offer version. Never bill rate, margin or burden."""
    bonus = fmt_money(version.bonus_amount, version.bonus_currency) if version.bonus_amount else ""
    flat = {
        "candidate.full_name": candidate.get("full_name") or "",
        "candidate.first_name": candidate.get("first_name") or "",
        "offer.title": version.title,
        "offer.employment_type": EMPLOYMENT_LABELS.get(version.employment_type, version.employment_type),
        "offer.pay_rate": fmt_money(Decimal(version.pay_rate), version.pay_currency),
        "offer.pay_rate_unit": UNIT_LABELS.get(version.pay_rate_unit, version.pay_rate_unit),
        "offer.bonus": bonus,
        "offer.benefits_summary": version.benefits_summary or "",
        "offer.start_date": fmt_date(version.start_date),
        "offer.end_date": fmt_date(version.end_date),
        "offer.location": version.location or "",
        "offer.work_mode": WORK_MODE_LABELS.get(version.work_mode or "", version.work_mode or ""),
        "offer.expiry_date": fmt_date(version.expiry_date),
        "offer.reference": f"{offer_ref} v{version.version_no}",
        "demand.title": demand_title or "",
        "client.name": client or "",
        "tenant.name": tenant_name,
        "signatory.name": signatory_name or "",
        "signatory.title": signatory_title or "",
        "today": fmt_date(today),
    }
    return _nest(flat)


def render_body(body: str, context: dict[str, Any]) -> str:
    """Render the Markdown body (HTML-escaped values). 422 on disallowed fields or render errors."""
    check_fields(body)
    try:
        return str(_ENV.from_string(body).render(**context))
    except (UndefinedError, SecurityError) as exc:
        raise ValidationFailed(
            "The template uses an unknown merge field", code="unknown_merge_field", details={"field": None}
        ) from exc
    except TemplateSyntaxError as exc:
        raise ValidationFailed(
            "The template has a syntax error", code="template_syntax_error", details={"line": exc.lineno}
        ) from exc


def validate_template(body: str) -> str:
    """Run a static check + sample render (template save / preview)."""
    return render_body(body, sample_context())


# ================================================================ defaults
_TERMS_COMMON = (
    "| Term | Detail |\n"
    "| --- | --- |\n"
    "| Position | {{ offer.title }} |\n"
    "| Employment type | {{ offer.employment_type }} |\n"
    "| Compensation | {{ offer.pay_rate }} {{ offer.pay_rate_unit }} |\n"
    "| Start date | {{ offer.start_date }} |\n"
)


def _default_body(employment_type: str) -> str:
    end_row = (
        "| End date | {{ offer.end_date }} |\n" if employment_type in ("contract", "contract_to_hire") else ""
    )
    intro = {
        "permanent": "We are delighted to offer you a permanent position",
        "contract": "We are pleased to offer you a contract engagement",
        "contract_to_hire": "We are pleased to offer you a contract-to-hire engagement",
        "freelance": "We are pleased to offer you a freelance engagement",
    }[employment_type]
    return (
        "# Offer of employment\n\n"
        "{{ today }}\n\n"
        "Dear {{ candidate.full_name }},\n\n"
        f"{intro} as **{{{{ offer.title }}}}** with {{{{ tenant.name }}}}.\n\n"
        "## Key terms\n\n"
        f"{_TERMS_COMMON}{end_row}"
        "| Location | {{ offer.location }} ({{ offer.work_mode }}) |\n\n"
        "## Benefits\n\n"
        "{{ offer.benefits_summary }}\n\n"
        "## Acceptance\n\n"
        "This offer is valid until **{{ offer.expiry_date }}**. To accept, sign electronically by typing your "
        "full name on the offer page.\n\n"
        "Sincerely,\n\n"
        "{{ signatory.name }}  \n"
        "{{ signatory.title }}, {{ tenant.name }}\n\n"
        "*Reference: {{ offer.reference }}*\n"
    )


DEFAULT_TEMPLATES: dict[str, tuple[str, str]] = {
    t: (f"{EMPLOYMENT_LABELS[t]} offer", _default_body(t)) for t in EMPLOYMENT_TYPES
}


async def ensure_default_templates(db: AsyncSession, tenant_id: uuid.UUID | None = None) -> int:
    """Seed one default template per employment type that has none (idempotent)."""
    tid = tenant_id or tenant_of(db)
    rows = list(
        (await db.execute(select(OfferTemplate).where(OfferTemplate.archived_at.is_(None)))).scalars()
    )
    covered = {t for r in rows for t in (r.employment_types or [])}
    created = 0
    for employment_type, (name, body) in DEFAULT_TEMPLATES.items():
        if employment_type in covered:
            continue
        db.add(
            OfferTemplate(
                id=uuid.uuid4(),
                tenant_id=tid,
                name=name,
                employment_types=[employment_type],
                body_md=body,
                is_default=True,
                version=1,
            )
        )
        created += 1
    if created:
        await db.flush()
    return created


async def default_template_for(db: AsyncSession, employment_type: str) -> OfferTemplate | None:
    await ensure_default_templates(db)
    rows = list(
        (
            await db.execute(
                select(OfferTemplate)
                .where(OfferTemplate.archived_at.is_(None))
                .order_by(OfferTemplate.is_default.desc(), OfferTemplate.created_at)
            )
        ).scalars()
    )
    for row in rows:
        if employment_type in (row.employment_types or []):
            return row
    return rows[0] if rows else None


async def get_template(db: AsyncSession, template_id: uuid.UUID) -> OfferTemplate:
    row = await db.get(OfferTemplate, template_id)
    if row is None:
        raise NotFound("Offer template not found")
    return row


# ================================================================ admin CRUD
def _clear_other_defaults(rows: Iterable[OfferTemplate], keep: OfferTemplate, types: list[str]) -> None:
    for row in rows:
        if row.id != keep.id and row.is_default and set(row.employment_types or []) & set(types):
            row.is_default = False  # one default template per employment type


async def list_templates(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    include_archived: bool = False,
) -> list[OfferTemplateOut]:
    await ensure_default_templates(db)
    stmt = select(OfferTemplate).order_by(OfferTemplate.name, OfferTemplate.created_at)
    if not include_archived:
        stmt = stmt.where(OfferTemplate.archived_at.is_(None))
    return [OfferTemplateOut.model_validate(r) for r in (await db.execute(stmt)).scalars()]


async def create_template(ctx: RequestContext, db: AsyncSession, data: OfferTemplateIn) -> OfferTemplateOut:  # ruff: ignore[unused-function-argument]
    validate_template(data.body_md)
    row = OfferTemplate(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name.strip(),
        employment_types=list(dict.fromkeys(data.employment_types)),
        body_md=data.body_md,
        is_default=data.is_default,
        version=1,
    )
    db.add(row)
    await db.flush()
    if row.is_default:
        others = (
            await db.execute(select(OfferTemplate).where(OfferTemplate.archived_at.is_(None)))
        ).scalars()
        _clear_other_defaults(others, row, row.employment_types)
        await db.flush()
    await db.refresh(row)
    return OfferTemplateOut.model_validate(row)


async def update_template(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    template_id: uuid.UUID,
    data: OfferTemplateUpdate,
) -> OfferTemplateOut:
    row = await get_template(db, template_id)
    if row.archived_at is not None:
        raise ValidationFailed("Archived templates cannot be edited", code="template_archived")
    changes = data.model_dump(exclude_unset=True)
    if changes.get("body_md") is not None:
        validate_template(changes["body_md"])
    for key in ("name", "employment_types", "body_md", "is_default"):
        value = changes.get(key)
        if value is None:
            continue
        if key == "name":
            value = value.strip()
        elif key == "employment_types":
            value = list(dict.fromkeys(value))
        setattr(row, key, value)
    row.version = row.version + 1
    await db.flush()
    if row.is_default:
        others = (
            await db.execute(select(OfferTemplate).where(OfferTemplate.archived_at.is_(None)))
        ).scalars()
        _clear_other_defaults(others, row, row.employment_types)
        await db.flush()
    await db.refresh(row)
    return OfferTemplateOut.model_validate(row)


async def archive_template(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    template_id: uuid.UUID,
) -> OfferTemplateOut:
    row = await get_template(db, template_id)
    if row.archived_at is None:
        row.archived_at = now()
        row.is_default = False
        await db.flush()
    await db.refresh(row)
    return OfferTemplateOut.model_validate(row)

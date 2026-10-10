# ruff: noqa: E501
"""The Talent OS field catalogue for HRMS mappings (Epic 71, spec A3): key -> label -> source. Sensitive keys (PAN,
bank account number / IFSC, Aadhaar reference) are sent only when a Finance user enabled the mapping with a purpose.

``placement_values`` reads one Placement's values (personal data stays in memory; never stored or logged).
"""

import dataclasses
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal, Scope
from app.models.candidates import Candidate, CandidateEmail, CandidatePhone
from app.models.clients import Client
from app.models.identity import User
from app.models.placements import Placement

__all__ = ["CATALOGUE", "SENSITIVE_KEYS", "CatalogueField", "employment_type", "placement_values"]


@dataclasses.dataclass(frozen=True, slots=True)
class CatalogueField:
    key: str
    label: str
    sensitive: bool = False


CATALOGUE: tuple[CatalogueField, ...] = (
    CatalogueField("full_name", "Full name"),
    CatalogueField("first_name", "First name"),
    CatalogueField("last_name", "Last name"),
    CatalogueField("work_email", "Work email"),
    CatalogueField("personal_email", "Personal email"),
    CatalogueField("phone", "Phone"),
    CatalogueField("date_of_joining", "Date of joining"),
    CatalogueField("designation", "Designation"),
    CatalogueField("department", "Department"),
    CatalogueField("cost_centre", "Cost centre"),
    CatalogueField("location", "Location"),
    CatalogueField("work_state", "Work state"),
    CatalogueField("employment_type", "Employment type"),
    CatalogueField("manager_email", "Manager email"),
    CatalogueField("placement_human_id", "Placement ID"),
    CatalogueField("client_name", "Client name"),
    CatalogueField("end_date", "End date"),
    CatalogueField("pay_currency", "Pay currency"),
    CatalogueField("pan", "PAN", sensitive=True),
    CatalogueField("bank_account_number", "Bank account number", sensitive=True),
    CatalogueField("bank_ifsc", "Bank IFSC", sensitive=True),
    CatalogueField("aadhaar_reference", "Aadhaar reference", sensitive=True),
)
BY_KEY = {f.key: f for f in CATALOGUE}
SENSITIVE_KEYS = frozenset(f.key for f in CATALOGUE if f.sensitive)


def employment_type(p: Placement) -> str:
    if p.engagement_kind in ("permanent", "c2h"):
        return str(p.engagement_kind)
    if p.worker_type == "employee" and p.engagement_kind is None:
        return "permanent"
    return "contract"


def _split(name: str) -> tuple[str, str]:
    name = name.strip()
    if " " not in name:
        return name, ""
    first, last = name.rsplit(" ", 1)
    return first, last


def _identity_ctx(tenant_id: uuid.UUID) -> RequestContext:
    """Narrow internal reader for the P2 identity accessor (sensitive mappings only; the accessor audits the unmask)."""
    principal = Principal(type="platform", id=uuid.UUID(int=0), tenant_id=tenant_id, permissions={"identity:view": Scope.TENANT})
    return RequestContext(tenant_id=tenant_id, principal=principal, actor_kind="system")


async def placement_values(db: AsyncSession, p: Placement, wanted: set[str]) -> dict[str, Any]:
    """Catalogue values of ``p`` for the ``wanted`` keys (``None`` when unknown)."""
    out: dict[str, Any] = {}
    cand = await db.get(Candidate, p.candidate_id)
    full = str(getattr(cand, "full_name", "") or "")
    first, last = _split(full)
    emails = list((await db.execute(select(CandidateEmail).where(CandidateEmail.candidate_id == p.candidate_id).order_by(CandidateEmail.is_primary.desc()))).scalars())
    phones = list((await db.execute(select(CandidatePhone).where(CandidatePhone.candidate_id == p.candidate_id).order_by(CandidatePhone.is_primary.desc()))).scalars())
    client = await db.get(Client, p.client_id)
    client_name = str(client.name) if client is not None else None
    project_name = None
    if p.project_id is not None and "department" in wanted:
        from app.models.projects import Project

        proj = await db.get(Project, p.project_id)
        project_name = proj.name if proj is not None else None
    manager_email = None
    if p.manager_user_id is not None and "manager_email" in wanted:
        user = await db.get(User, p.manager_user_id)
        manager_email = str(user.email) if user is not None else None
    values: dict[str, Any] = {
        "full_name": full or None,
        "first_name": first or None,
        "last_name": last or None,
        "work_email": str(emails[0].email) if emails else None,
        "personal_email": str(emails[1].email) if len(emails) > 1 else (str(emails[0].email) if emails else None),
        "phone": phones[0].phone_e164 if phones else None,
        "date_of_joining": p.actual_start_date or p.planned_start_date,
        "designation": p.role_title,
        "department": project_name or client_name,
        "cost_centre": client_name,
        "location": p.work_location_text,
        "work_state": p.work_state_code,
        "employment_type": employment_type(p),
        "manager_email": manager_email,
        "placement_human_id": p.human_id,
        "client_name": client_name,
        "end_date": p.end_date,
        "pay_currency": p.currency,
    }
    for key in wanted:
        if key in values:
            out[key] = values[key]
    sensitive = wanted & SENSITIVE_KEYS
    if sensitive:
        from app.services import placements_service

        ident = None
        try:
            ident = await placements_service.worker_payment_identity(_identity_ctx(p.tenant_id), db, p.candidate_id, unmask=bool(sensitive - {"bank_ifsc"}))
        except Exception:  # the accessor refuses or the profile is missing: send nothing
            ident = None
        out["pan"] = getattr(ident, "pan", None)
        out["bank_account_number"] = getattr(ident, "account_number", None)
        out["bank_ifsc"] = getattr(ident, "ifsc", None)
        out["aadhaar_reference"] = None  # the reference id is not held by P2 (sent only once a source exists)
        for key in SENSITIVE_KEYS - sensitive:
            out.pop(key, None)
    return out


def iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value

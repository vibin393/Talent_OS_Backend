# ruff: noqa: E501, D401
"""Epic 63 collections test helpers.

``seed_collections(admin, tenant)`` builds, through the API: the default Legal Entity, a Client with a billing profile
whose billing contact (``to``) is "Asha Payables", and a second Contact "Chitra CFO" (the escalation Contact of the
Client rule when ``client_rule=True``). Issued invoices are inserted directly (billing tables are only read here).
The Collections Agent stays off unless ``enable_agent`` is called.
"""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.services import ai
from tests.modules.demands.helpers import create_client, h
from tests.modules.identity.conftest import Member
from tests.modules.legal_entities.conftest import provision

API = "/api/v1"
_n = 0


@dataclasses.dataclass
class CollSeed:
    tenant_id: uuid.UUID
    admin: Member
    entity_id: uuid.UUID
    client_id: uuid.UUID
    billing_contact_id: uuid.UUID
    cfo_contact_id: uuid.UUID
    rule_id: uuid.UUID | None = None

    def values(self, **extra: str) -> dict[str, str]:
        return {
            "client_id": str(self.client_id), "owner_id": str(self.admin.user_id), "subject_type": "client",
            "subject_id": str(self.client_id), "subject_candidate_ids": "", "random_id": str(uuid.uuid4()),
            "billing_contact_id": str(self.billing_contact_id), "cfo_contact_id": str(self.cfo_contact_id), **extra,
        }  # fmt: skip


async def _contact(admin: Member, client_id: uuid.UUID, name: str, email: str) -> uuid.UUID:
    resp = await admin.client.post(
        f"{API}/clients/{client_id}/contacts", json={"name": name, "email": email, "type": "other"}, headers=h(admin)
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def seed_collections(admin: Member, tenant: Any, *, client_rule: bool = True, allow_auto: bool = False) -> CollSeed:
    async with tenant_session(tenant.id) as db:
        from app.models.legal_entities import LegalEntity

        existing = (await db.execute(select(LegalEntity.id).limit(1))).scalar_one_or_none()
    entity_id = existing or await provision(tenant)
    async with tenant_session(tenant.id) as db:
        from app.services import legal_entities_service

        await legal_entities_service.ensure_periods(db, entity_id, date(2025, 12, 1), date(2027, 3, 1))
    client_id = await create_client(admin, f"Acme {uuid.uuid4().hex[:5]}")
    tag = uuid.uuid4().hex[:6]
    ap = await _contact(admin, client_id, "Asha Payables", f"ap-{tag}@acme.example.com")
    cfo = await _contact(admin, client_id, "Chitra CFO", f"cfo-{tag}@acme.example.com")
    resp = await admin.client.put(
        f"{API}/clients/{client_id}/billing-profile",
        json={"legal_name": f"Acme {tag} Pvt Ltd", "pan": "AAACL1234C", "tax_treatment": "registered"}, headers=h(admin),
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    resp = await admin.client.put(f"{API}/clients/{client_id}/billing-profile/contacts", json={"to": [str(ap)], "cc": []}, headers=h(admin))
    assert resp.status_code == 200, resp.text
    seed = CollSeed(tenant_id=tenant.id, admin=admin, entity_id=entity_id, client_id=client_id, billing_contact_id=ap, cfo_contact_id=cfo)
    if client_rule:
        seed.rule_id = await client_rule_for(seed, allow_auto=allow_auto)
    return seed


async def client_rule_for(seed: CollSeed, *, allow_auto: bool = False, **rule: Any) -> uuid.UUID:
    templates = await templates_of(seed)
    body = {
        "client_id": str(seed.client_id), "name": "Acme reminders", "escalation_contact_id": str(seed.cfo_contact_id),
        "max_reminders_per_week": 7, "quiet_start": "23:59", "quiet_end": "00:00",
        "steps": [
            {"position": 1, "offset_days": -3, "tone": "courteous", "audience": "billing_contacts", "email_template_id": templates["courteous"], "allow_auto": allow_auto},
            {"position": 2, "offset_days": 7, "tone": "friendly", "audience": "billing_contacts", "email_template_id": templates["friendly"]},
            {"position": 3, "offset_days": 21, "tone": "firm", "audience": "billing_contacts", "email_template_id": templates["firm"]},
            {"position": 4, "offset_days": 30, "tone": "escalation", "audience": "escalation_contact", "create_task": True},
        ],
        **rule,
    }  # fmt: skip
    resp = await seed.admin.client.post(f"{API}/collections/rules", json=body, headers=h(seed.admin))
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def templates_of(seed: CollSeed) -> dict[str, str]:
    from app.services.collection_rules import rules

    async with tenant_session(seed.tenant_id) as db:
        ids = await rules.ensure_templates(db)
    return {k: str(v) for k, v in ids.items()}


def _number() -> str:
    global _n  # test-local counter
    _n += 1
    return f"INV{uuid.uuid4().hex[:5].upper()}{_n}"[:16]


async def invoice(seed: CollSeed, total: str = "118000.00", *, due_in_days: int = -8, today: date | None = None) -> uuid.UUID:
    """An issued invoice due ``due_in_days`` from today (negative = overdue)."""
    from app.models.billing import Invoice

    base = today or date.today()
    due = base + timedelta(days=due_in_days)
    values: dict[str, Any] = {
        "id": uuid.uuid4(), "tenant_id": seed.tenant_id, "legal_entity_id": seed.entity_id, "client_id": seed.client_id,
        "number": _number(), "doc_date": due - timedelta(days=30), "due_date": due, "status": "issued", "currency": "INR",
        "document_total": Decimal(total), "taxable_total": Decimal(total), "inr_document_total": Decimal(total), "fiscal_year": "26-27",
    }  # fmt: skip
    async with tenant_session(seed.tenant_id) as db:
        db.add(Invoice(**values))
    return values["id"]


async def dispute(seed: CollSeed, invoice_id: uuid.UUID, amount: str) -> None:
    resp = await seed.admin.client.post(
        f"{API}/receivables/disputes",
        json={"document_kind": "invoice", "document_id": str(invoice_id), "amount": amount, "reason": "Rate query"},
        headers=h(seed.admin),
    )  # fmt: skip
    assert resp.status_code == 201, resp.text


async def plan(seed: CollSeed) -> dict[str, Any]:
    resp = await seed.admin.client.get(f"{API}/collections/clients/{seed.client_id}/plan")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def enable_agent(tenant_id: uuid.UUID, on: bool = True) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, "agent_collections": on})


async def rows(tenant_id: uuid.UUID, model: Any, *where: Any) -> list[Any]:
    async with tenant_session(tenant_id) as db:
        out = list((await db.execute(select(model).where(*where))).scalars())
        for r in out:
            db.expunge(r)
        return out


__all__ = ["API", "CollSeed", "client_rule_for", "dispute", "enable_agent", "invoice", "plan", "rows", "seed_collections", "templates_of"]

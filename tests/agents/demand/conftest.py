# ruff: noqa: E501
"""Epic 55 Demand Agent test harness.

* ``dorg`` -- a tenant with ``agentic`` + ``agent_demand`` on (no burn-in: the feature is set directly), its Tenant
  Admin (the Account Manager / Client owner), the client "Acme Analytics" with the contact Priya
  (``priya@acme.example``) and intake settings (``platform_address``, fallback owner = the admin).
* ``dorg_other`` -- a second tenant (cross-tenant isolation).
* Model calls use the scripted Fake LLM (``fake_llm``); ``anthropic_api_key`` is a dummy so the one-shot gateway reaches
  the fake too. Inbound mail is built in memory (``receive.build_eml``) and POSTed signed with the test secret --
  nothing is ever sent to a real address.
"""

import dataclasses
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.services import ai
from app.services.ai.fake_llm import FakeTurn
from tests.modules.identity.conftest import Member, csrf, join, make_tenant

WEBHOOK_SECRET = "test-intake-secret"
INTAKE_DOMAIN = "intake.talentos.test"
SCRIPTS = Path(__file__).resolve().parents[3] / "evals" / "demand" / "scripts"


@dataclasses.dataclass
class DOrg:
    tenant_id: uuid.UUID
    admin: Member
    client_id: uuid.UUID
    contact_id: uuid.UUID
    token: str | None = None

    def ctx(self, user_id: uuid.UUID | None = None) -> RequestContext:
        from app.core.permissions import CATALOG, Scope

        principal = Principal(
            type="user",
            id=user_id or self.admin.user_id,
            tenant_id=self.tenant_id,
            permissions=dict.fromkeys(CATALOG, Scope.TENANT),
        )
        return RequestContext(tenant_id=self.tenant_id, principal=principal)

    @property
    def address(self) -> str:
        return f"req-{self.token}@{INTAKE_DOMAIN}"


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


@pytest.fixture(autouse=True)
def _demand_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", "test-key-not-used")
    monkeypatch.setattr(s, "demand_intake_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(s, "demand_intake_email_domain", INTAKE_DOMAIN)
    yield


async def create_client(m: Member, name: str) -> uuid.UUID:
    resp = await m.client.post("/api/v1/clients", json={"name": name}, headers=h(m))
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def create_contact(m: Member, client_id: uuid.UUID, name: str, email: str) -> uuid.UUID:
    resp = await m.client.post(
        f"/api/v1/clients/{client_id}/contacts", json={"name": name, "email": email}, headers=h(m)
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def enable_demand_agent(tenant_id: uuid.UUID, **extra: bool) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, "agent_demand": True, **extra})


async def disable_triggers(org: DOrg, *keys: str) -> None:
    from app.schemas.agentic import AgentConfigIn
    from app.services import agent_runtime_service as svc

    async with tenant_session(org.tenant_id) as db:
        await svc.update_config(
            org.ctx(), db, "demand", AgentConfigIn(triggers_enabled=dict.fromkeys(keys, False))
        )


async def _org(app: FastAPI, name: str, client_name: str) -> DOrg:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name=name)
    client_id = await create_client(admin, client_name)
    contact_id = await create_contact(
        admin, client_id, "Priya Sharma", f"priya@{client_name.split()[0].lower()}.example"
    )
    await enable_demand_agent(tenant.id)
    org = DOrg(tenant_id=tenant.id, admin=admin, client_id=client_id, contact_id=contact_id)
    resp = await admin.client.put(
        "/api/v1/demands/intake-settings",
        json={"intake_mode": "platform_address", "fallback_owner_id": str(admin.user_id)},
        headers=h(admin),
    )
    assert resp.status_code == 200, resp.text
    org.token = resp.json()["platform_address"].split("@")[0].removeprefix("req-")
    return org


@pytest.fixture
async def dorg(app: FastAPI) -> AsyncIterator[DOrg]:
    org = await _org(app, "Ada Admin", "Acme Analytics")
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def dorg_other(app: FastAPI) -> AsyncIterator[DOrg]:
    org = await _org(app, "Olga Other", "Globex Corp")
    yield org
    await org.admin.client.aclose()


# ================================================================ inbound mail (never sent anywhere)
def eml(org: DOrg, *, sender: str, subject: str = "Requirement", body: str, message_id: str | None = None,
        attachments: tuple[tuple[str, str, bytes], ...] = (), auth: str | None = None, html: bool = False) -> bytes:  # fmt: skip
    from app.services.demand_intake.receive import build_eml

    return build_eml(
        from_addr=sender,
        to=org.address,
        subject=subject,
        body=body,
        message_id=message_id,
        attachments=attachments,
        auth_results=auth,
        html=html,
    )


async def post_inbound(app: FastAPI, raw: bytes, recipient: str, *, secret: str = WEBHOOK_SECRET) -> Any:
    from app.services.demand_intake.routes import signature_for
    from tests.modules.identity.conftest import new_client

    async with new_client(app) as client:
        return await client.post(
            "/api/v1/inbound/demand-intake", content=raw,
            headers={"X-Intake-Signature": signature_for(raw, secret), "X-Intake-Recipient": recipient, "Content-Type": "message/rfc822"},
        )  # fmt: skip


async def message_row(org: DOrg, message_id: uuid.UUID) -> Any:
    from app.models.demand_intake import DemandIntakeMessage

    async with tenant_session(org.tenant_id) as db:
        row = await db.get(DemandIntakeMessage, message_id)
        assert row is not None
        db.expunge(row)
        return row


# ================================================================ Fake LLM scripts (evals/demand/scripts)
def _fill(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\{\{(\w+)\}\}", lambda m: ids.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_fill(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value


def script(name: str, **ids: Any) -> dict[str, list[FakeTurn]]:
    """Load ``evals/demand/scripts/<name>.yaml`` (prompt id -> turns) with ``{{placeholders}}`` filled."""
    raw = yaml.safe_load((SCRIPTS / f"{name}.yaml").read_text(encoding="utf-8"))
    turns = raw.get("turns", raw)
    filled = _fill(turns, {k: str(v) for k, v in ids.items()})
    return {prompt: [FakeTurn.from_mapping(t) for t in items] for prompt, items in filled.items()}


async def start_demand_run(
    org: DOrg,
    trigger_key: str | None,
    subject: tuple[str, uuid.UUID] | None,
    *,
    owner: uuid.UUID | None = None,
    kind: str = "event",
) -> Any:
    """Start a Demand Agent run the way its trigger would (same kickoff mode), but under a test dedup key and with the
    real trigger disabled-safe: the run's own ``trigger_key`` is left empty so ``triggers_enabled`` cannot refuse it."""
    from app.services import agent_runtime_service as runtime
    from app.services.agents.demand.kickoff import kickoff

    payload = {
        "subject_type": subject[0] if subject else None,
        "subject_id": str(subject[1]) if subject else None,
        "trigger_key": trigger_key,
    }
    async with tenant_session(org.tenant_id) as db:
        return await runtime.start_run(
            org.ctx(), db, agent_key="demand", trigger_kind=kind, trigger_key=None, subject=subject, kickoff=kickoff(payload),
            owner_user_id=owner, dedup_key=f"test:{uuid.uuid4()}",
        )  # fmt: skip

"""Shared helpers for the Epic 22 documents & eSignature tests."""

import re
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.mailer import EmailMessage, get_mailer
from app.core.settings import get_settings
from app.dependencies.candidate import current_candidate
from tests.epic3 import make_pdf, upload
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import Member, join
from tests.modules.offers.helpers import create_candidate

DOCS = "/api/v1/documents"
LINKS = "/api/v1/public/links"
WORK = "/api/v1/portal/work/signatures"

BODY = """# Employment agreement

This agreement is between **{{ tenant.name }}** and {{ worker.full_name }}.

- Role: {{ placement.role }}
- Start date: {{ placement.start_date | date }}
- Pay: {{ rate_segment.pay_rate | money(rate_segment.currency) }}

Signed for {{ tenant.name }} by {{ signatory.name }}, {{ signatory.title }}.
"""

MERGE = {
    "worker": {"full_name": "Asha Rao"},
    "placement": {"role": "Data Engineer", "start_date": "2026-11-02"},
    "rate_segment": {"pay_rate": "1200", "currency": "INR"},
    "signatory": {"name": "Ada Admin", "title": "Director"},
}


def frontend() -> str:
    return get_settings().frontend_url.rstrip("/")


def post_headers() -> dict[str, str]:
    return {"Origin": frontend(), "User-Agent": "pytest-browser/1.0"}


def emails(tenant_id: uuid.UUID, kind: str | None = None, to: str | None = None) -> list[EmailMessage]:
    sent = list(get_mailer().sent)  # type: ignore[attr-defined]
    return [
        m
        for m in sent
        if m.tenant_id == tenant_id and (kind is None or m.kind == kind) and (to is None or m.to == to)
    ]


def last_code(tenant_id: uuid.UUID, to: str) -> str:
    (msg,) = emails(tenant_id, "esign_otp", to)[-1:]
    match = re.search(r"\b(\d{6})\b", msg.subject)
    assert match, msg.subject
    return match.group(1)


def link_token(message: EmailMessage) -> str:
    marker = f"{frontend()}/l/"
    start = message.text.index(marker) + len(marker)
    end = start
    while end < len(message.text) and not message.text[end].isspace():
        end += 1
    return message.text[start:end]


@pytest.fixture
def as_candidate(app: FastAPI) -> Iterator[Any]:
    def _set(tenant_id: Any, candidate_id: Any) -> None:
        principal = SimpleNamespace(tenant_id=tenant_id, candidate_id=candidate_id, id=candidate_id)
        app.dependency_overrides[current_candidate] = lambda: principal

    yield _set
    app.dependency_overrides.pop(current_candidate, None)


async def published_template(m: Member, body: str = BODY, **extra: Any) -> dict[str, Any]:
    resp = await m.client.post(
        f"{DOCS}/templates",
        json={"name": "Employment agreement", "kind": "employment_agreement", "body_md": body, **extra},
        headers=h(m),
    )
    assert resp.status_code == 201, resp.text
    template = resp.json()
    vid = template["versions"][0]["id"]
    pub = await m.client.post(f"{DOCS}/templates/versions/{vid}/publish", headers=h(m))
    assert pub.status_code == 200, pub.text
    return dict(pub.json())


async def candidate_with_email(org: DOrg, name: str = "Asha Rao") -> tuple[uuid.UUID, str]:
    email = f"cand-{uuid.uuid4().hex[:8]}@example.com"
    return await create_candidate(org.admin, name, email), email


async def send_request(
    org: DOrg, template_id: str, candidate_id: uuid.UUID, *, extra_signers: list[dict[str, Any]] | None = None, **body: Any
) -> dict[str, Any]:
    resp = await org.admin.client.post(
        f"{DOCS}/signature-requests",
        json={
            "template_id": template_id,
            "merge_data": MERGE,
            "owner": {"kind": "candidate", "id": str(candidate_id)},
            "signers": [{"kind": "candidate", "principal_id": str(candidate_id)}, *(extra_signers or [])],
            **body,
        },
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def upload_doc(org: DOrg, owner_id: uuid.UUID, **form: str) -> dict[str, Any]:
    resp = await upload(
        org.admin.client, DOCS, make_pdf("Signed contract body"), "contract.pdf",
        owner_kind="candidate", owner_id=str(owner_id), category=form.pop("category", "other"), **form,
    )  # fmt: skip
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def member_with(app: FastAPI, org: DOrg, *perms: str, key: str | None = None) -> Member:
    key = key or f"r{uuid.uuid4().hex[:8]}"
    resp = await org.admin.client.post(
        "/api/v1/admin/roles",
        json={"key": key, "name": f"Role {key}", "permissions": dict.fromkeys(perms, "tenant")},
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text
    return await join(app, org.tenant, key, name="Rita Reader")

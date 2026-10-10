"""Shared fixtures / helpers for the Epic 3 tests (candidates, ai, files).

* Files go to a temporary ``LocalStorage`` (never ``Backend/data/files``).
* The AI gateway never touches the network: the API key is blanked by default and ``fake_ai`` installs a
  fake Anthropic client via ``ai.set_client_factory``.
* Resume fixtures (PDF / DOCX) are generated in-test with pypdf / python-docx.
"""

import dataclasses
import io
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core import storage
from app.core.settings import get_settings
from app.services import ai
from tests.modules.identity.conftest import Member, csrf, join, make_tenant

CANDIDATES = "/api/v1/candidates"


# ================================================================ environment
@pytest.fixture(autouse=True)
def _isolated_files(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("files")
    storage.set_storage(storage.LocalStorage(root))
    yield root
    storage.set_storage(None)


@pytest.fixture(autouse=True)
def _no_real_ai(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", "")
    monkeypatch.setattr(s, "clamav_host", "")
    # Epic 3 suites predate the privacy consent checker (Epic 12 installs it as the gateway default): they
    # run with the allow-all checker; the privacy tests exercise the real one.
    ai.set_consent_checker(ai.allow_all_consent_checker())
    yield
    ai.set_client_factory(None)
    ai.set_consent_checker(None)


# ================================================================ fake AI
@dataclasses.dataclass
class FakeAI:
    payload: dict[str, Any] | None = None
    error: BaseException | None = None
    input_tokens: int = 120
    output_tokens: int = 80
    calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Optional: compute the tool output from the user message (e.g. per imported file).
    payload_for: Callable[[str], dict[str, Any]] | None = None

    def factory(self, api_key: str) -> Any:
        fake = self

        class _Messages:
            async def create(self, **kwargs: Any) -> Any:
                fake.calls.append(kwargs)
                if fake.error is not None:
                    raise fake.error
                payload = fake.payload or {}
                if fake.payload_for is not None:
                    payload = fake.payload_for(kwargs["messages"][0]["content"])
                block = SimpleNamespace(type="tool_use", name="emit_result", input=payload)
                usage = SimpleNamespace(input_tokens=fake.input_tokens, output_tokens=fake.output_tokens)
                return SimpleNamespace(content=[block], usage=usage)

        assert api_key == "test-key"
        return SimpleNamespace(messages=_Messages())


@pytest.fixture
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAI]:
    controller = FakeAI(payload=parsed_payload())
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    ai.set_client_factory(controller.factory)
    yield controller
    ai.set_client_factory(None)


def parsed_payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "full_name": "Priya Raman",
        "emails": ["Priya.Raman@Example.com", "not-an-email"],
        "phones": ["98765 43210", "12"],
        "location": {"city": "Chennai", "state": "Tamil Nadu", "country": "India"},
        "links": [{"url": "https://www.linkedin.com/in/priya-raman/?trk=x", "kind": "linkedin"}],
        "current_title": "Data Engineer",
        "current_employer": "Infosys",
        "total_experience_months": 62,
        "skills": [
            {"name": "ADF", "years": 3, "last_used": "2026-08"},
            {"name": "PySpark", "years": 2.5},
            {"name": "FooLangXYZ", "years": 1},
        ],
        "employment": [
            {"employer": "Infosys", "title": "Data Engineer", "start_date": "2022-01", "is_current": True},
            {"employer": "TCS", "title": "ETL Developer", "start_date": "2019-06", "end_date": "2021-12"},
        ],
        "projects": [{"name": "Lakehouse migration", "technologies": ["ADF", "Databricks"]}],
        "education": [{"institution": "Anna University", "degree": "B.E.", "field": "CSE", "end_year": 2019}],
        "certifications": [{"name": "DP-203", "issuer": "Microsoft"}],
        "limitations_note": None,
    }
    body.update(overrides)
    return body


# ================================================================ documents
def make_pdf(text: str | None = None) -> bytes:
    """Build a one-page PDF: with ``text`` it has a text layer, without it is image-only."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        lines = []
        for i, line in enumerate(text.splitlines() or [text]):
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"BT /F1 11 Tf 50 {740 - 16 * i} Td ({safe}) Tj ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(lines).encode("latin-1", "replace"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


RESUME_TEXT = (
    "Priya Raman\nData Engineer at Infosys, Chennai\npriya.raman@example.com  +91 98765 43210\n"
    "Skills: Azure Data Factory, PySpark, SQL\nExperience: Infosys 2022-present, TCS 2019-2021\n"
)


def resume_pdf(text: str = RESUME_TEXT) -> bytes:
    return make_pdf(text)


def make_docx(text: str = RESUME_TEXT) -> bytes:
    import docx

    document = docx.Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def make_zip(entries: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ================================================================ orgs
@dataclasses.dataclass
class Org3:
    tenant_id: uuid.UUID
    admin: Member
    tenant: Any


@pytest.fixture
async def org3(app: FastAPI) -> AsyncIterator[Org3]:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Ada Admin")
    yield Org3(tenant_id=tenant.id, admin=admin, tenant=tenant)
    await admin.client.aclose()


@pytest.fixture
async def other3(app: FastAPI) -> AsyncIterator[Org3]:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name="Olga Other")
    yield Org3(tenant_id=tenant.id, admin=admin, tenant=tenant)
    await admin.client.aclose()


async def member(app: FastAPI, org: Org3, *roles: str, name: str = "Rita Recruiter") -> Member:
    return await join(app, org.tenant, *roles, name=name)


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


async def create_candidate(m: Member, **body: Any) -> dict[str, Any]:
    payload = {"full_name": "Rahul Sharma", **body}
    resp = await m.client.post(CANDIDATES, json=payload, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def upload(
    client: httpx.AsyncClient, url: str, data: bytes, filename: str = "cv.pdf", **form: str
) -> httpx.Response:
    return await client.post(
        url,
        files={"file": (filename, data, "application/octet-stream")},
        data=form or None,
        headers=csrf(client),
    )

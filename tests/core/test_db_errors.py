"""Epic 13: database guard SQLSTATEs become 409 domain errors (``core.db_errors``), also through the app's
exception handler."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.core.db_errors import translate
from app.core.errors import ConflictError, register_exception_handlers


async def _raise(code: str, message: str) -> None:
    async with tenant_session(uuid.uuid4()) as s:
        await s.execute(text(f"DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = '{code}', MESSAGE = '{message}'; END $$"))


async def test_translate_period_closed() -> None:
    le = uuid.uuid4()
    with pytest.raises(DBAPIError) as exc:
        await _raise("TOS01", f"period_closed:{le}:2026-04")
    domain = translate(exc.value)
    assert isinstance(domain, ConflictError)
    assert (domain.status_code, domain.code) == (409, "period_closed")
    assert domain.details == {"legal_entity_id": str(le), "period": "2026-04"}


async def test_translate_document_immutable() -> None:
    with pytest.raises(DBAPIError) as exc:
        await _raise("TOS02", "document_immutable:invoices")
    domain = translate(exc.value)
    assert domain is not None and domain.code == "document_immutable" and domain.details == {"table": "invoices"}


async def test_translate_exclusion_violation() -> None:
    with pytest.raises(DBAPIError) as exc:
        await _raise("23P01", "conflicting key value violates exclusion constraint")
    domain = translate(exc.value)
    assert domain is not None and domain.code == "overlap"


async def test_other_errors_are_not_translated() -> None:
    with pytest.raises(DBAPIError) as exc:
        await _raise("P0001", "boom")
    assert translate(exc.value) is None
    assert translate(RuntimeError("x")) is None


@pytest.fixture
async def guard_client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/immutable")
    async def immutable() -> None:
        await _raise("TOS02", "document_immutable:debit_notes")

    @app.get("/other")
    async def other() -> None:
        await _raise("P0001", "boom")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_handler_renders_409_envelope(guard_client: httpx.AsyncClient) -> None:
    resp = await guard_client.get("/immutable")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "document_immutable"
    assert resp.json()["error"]["details"] == {"table": "debit_notes"}
    other = await guard_client.get("/other")
    assert other.status_code == 500
    assert other.json()["error"]["code"] == "internal_error"

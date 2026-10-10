"""AD-18: gap-free statutory numbering (Epic 13, story 13.3), incl. the 50-way concurrent issue test."""

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.core.errors import ConflictError, ValidationFailed
from app.models.legal_entities import DocumentSeries, GstRegistration
from app.services.legal_entities import numbering
from app.services.legal_entities.numbering import (
    SeriesConfig,
    max_rendered_length,
    render,
    validate_series_config,
)
from tests.modules.identity.conftest import make_tenant
from tests.modules.legal_entities.conftest import gstin, provision


def test_render_formats() -> None:
    assert render(SeriesConfig("INV", "YY-YY", "/", 4), "26-27", 7) == "INV/26-27/0007"
    assert render(SeriesConfig("INV", "YYYY-YY", "-", 3), "26-27", 12) == "INV-2026-27-012"
    assert render(SeriesConfig("", "YY", "/", 5), "26-27", 1) == "26/00001"
    assert render(SeriesConfig("CN", "none", "", 6), "26-27", 42) == "CN000042"


def test_max_length_and_validation() -> None:
    assert max_rendered_length(SeriesConfig("INV", "YY-YY", "/", 4)) == 14
    long = SeriesConfig("ABCDEFGHIJ", "YYYY-YY", "/", 5)
    assert max_rendered_length(long) == 24
    assert [e.code for e in validate_series_config(long)] == ["series_too_long"]
    assert [e.code for e in validate_series_config(SeriesConfig("IN V", "YY", "/", 4))] == ["series_bad_chars"]
    assert validate_series_config(SeriesConfig("INV", "YY-YY", "/", 4)) == []


async def _series(prefix: str = "T", *, allow_backdated: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await make_tenant()
    entity_id = await provision(tenant)
    async with tenant_session(tenant.id) as db:
        reg = GstRegistration(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_entity_id=entity_id, gstin=gstin("29"), state_code="29", is_principal=True
        )
        db.add(reg)
        await db.flush()
        series = DocumentSeries(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            legal_entity_id=entity_id,
            gst_registration_id=reg.id,
            doc_type="invoice",
            name="Invoices",
            prefix=prefix,
            fy_format="YY-YY",
            separator="/",
            pad=4,
            allow_backdated=allow_backdated,
            status="active",
        )
        db.add(series)
    return tenant.id, series.id


async def test_assign_sequence_and_financial_years() -> None:
    tenant_id, series_id = await _series()
    async with tenant_session(tenant_id) as db:
        assert await numbering.preview_next(db, series_id, date(2026, 6, 1)) == "T/26-27/0001"
        assert await numbering.assign(db, series_id, date(2026, 6, 1)) == "T/26-27/0001"
        assert await numbering.assign(db, series_id, date(2026, 6, 2)) == "T/26-27/0002"
        # A new financial year restarts at 1.
        assert await numbering.assign(db, series_id, date(2027, 4, 1)) == "T/27-28/0001"
        assert await numbering.preview_next(db, series_id, date(2026, 6, 3)) == "T/26-27/0003"
    # Rolled-back assignment returns its number.
    with pytest.raises(RuntimeError):
        async with tenant_session(tenant_id) as db:
            assert await numbering.assign(db, series_id, date(2026, 6, 3)) == "T/26-27/0003"
            raise RuntimeError("issue failed")
    async with tenant_session(tenant_id) as db:
        assert await numbering.assign(db, series_id, date(2026, 6, 3)) == "T/26-27/0003"
        await db.flush()
        audit = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'number_assigned' AND entity_id = :s"), {"s": str(series_id)})).scalar_one()
        assert audit == 4


async def test_backdating() -> None:
    tenant_id, series_id = await _series()
    async with tenant_session(tenant_id) as db:
        await numbering.assign(db, series_id, date(2026, 9, 10))
        with pytest.raises(ValidationFailed) as exc:
            await numbering.assign(db, series_id, date(2026, 9, 1))
        assert exc.value.code == "doc_date_before_last_issued"
    tenant_id, series_id = await _series(allow_backdated=True)
    async with tenant_session(tenant_id) as db:
        await numbering.assign(db, series_id, date(2026, 9, 10))
        assert await numbering.assign(db, series_id, date(2026, 9, 1)) == "T/26-27/0002"
        last = (await db.execute(text("SELECT last_doc_date FROM document_series_counters WHERE series_id = :s"), {"s": series_id})).scalar_one()
        assert last == date(2026, 9, 10)  # GREATEST keeps the latest issued date


async def test_inactive_series_refused() -> None:
    tenant_id, series_id = await _series()
    async with tenant_session(tenant_id) as db:
        await db.execute(text("UPDATE document_series SET status = 'inactive' WHERE id = :s"), {"s": series_id})
        with pytest.raises(ConflictError) as exc:
            await numbering.assign(db, series_id, date(2026, 9, 1))
        assert exc.value.code == "series_inactive"


async def test_fifty_concurrent_assigns_with_rollbacks_are_gap_free() -> None:
    tenant_id, series_id = await _series("C")
    rollback = {i for i in range(50) if i % 5 == 3}  # 10 of 50 roll back (~20 %)
    committed: list[str] = []

    class _Abort(Exception):
        pass

    async def issue(i: int) -> None:
        try:
            async with tenant_session(tenant_id) as db:
                number = await numbering.assign(db, series_id, date(2026, 7, 1))
                await asyncio.sleep(0.005 * (i % 4))
                if i in rollback:
                    raise _Abort
            committed.append(number)
        except _Abort:
            pass

    await asyncio.gather(*(issue(i) for i in range(50)))
    assert len(committed) == 40
    assert sorted(committed) == [f"C/26-27/{n:04d}" for n in range(1, 41)]
    async with tenant_session(tenant_id) as db:
        last = (await db.execute(text("SELECT last_value FROM document_series_counters WHERE series_id = :s"), {"s": series_id})).scalar_one()
    assert last == 40


async def test_series_config_immutable_once_used() -> None:
    tenant_id, series_id = await _series()
    async with tenant_session(tenant_id) as db:
        await db.execute(text("UPDATE document_series SET prefix = 'X' WHERE id = :s"), {"s": series_id})  # unused: fine
        await numbering.assign(db, series_id, date(2026, 6, 1))
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(tenant_id) as db:
            await db.execute(text("UPDATE document_series SET pad = 6 WHERE id = :s"), {"s": series_id})
    assert "document_immutable:document_series" in str(exc.value)
    async with tenant_session(tenant_id) as db:
        # Non-format columns stay editable.
        await db.execute(text("UPDATE document_series SET name = 'Renamed' WHERE id = :s"), {"s": series_id})
        from app.core.context import RequestContext
        from app.schemas.legal_entities import DocumentSeriesUpdate
        from app.services import legal_entities_service

        with pytest.raises(ConflictError) as conflict:
            await legal_entities_service.update_series(
                RequestContext(tenant_id=tenant_id), db, series_id, DocumentSeriesUpdate(prefix="Y")
            )
        assert conflict.value.code == "series_in_use"


async def test_raise_counter_for_imports() -> None:
    tenant_id, series_id = await _series()
    async with tenant_session(tenant_id) as db:
        await numbering.raise_counter_to(db, series_id, "26-27", 120)
        assert await numbering.assign(db, series_id, date(2026, 8, 1)) == "T/26-27/0121"
        await numbering.raise_counter_to(db, series_id, "26-27", 5)  # never lowers
        assert await numbering.assign(db, series_id, date(2026, 8, 1)) == "T/26-27/0122"

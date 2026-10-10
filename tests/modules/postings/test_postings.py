"""AD-23 posting ledgers: the single writer ``services/postings.py`` (Epic 13, story 13.8)."""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, ValidationFailed
from app.services import postings
from app.services.postings import PostingLine, SourceItem, SourceQuery
from tests.core.guard_helpers import sqlstate
from tests.modules.identity.conftest import make_tenant


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id)


def _line(source_id: uuid.UUID, *, document_id: uuid.UUID | None = None, kind: str = "manual") -> PostingLine:
    return PostingLine(
        source_kind=kind,
        source_id=source_id,
        target_kind="invoice_line",
        target_id=uuid.uuid4(),
        document_id=document_id,
        quantity=Decimal("8.0000"),
        amount=Decimal("800.00"),
        currency="INR",
    )


async def _tenant() -> uuid.UUID:
    return (await make_tenant()).id


async def test_reserve_post_release_and_reserve_again() -> None:
    tenant_id = await _tenant()
    ctx = _ctx(tenant_id)
    source, doc = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        ids = await postings.reserve(ctx, db, "billing", [_line(source, document_id=doc)])
        assert len(ids) == 1
        assert await postings.has_active_posting(db, "manual", source)
        assert await postings.post(ctx, db, "billing", document_id=doc) == 1
        rows = await postings.targets_for(db, "billing", "manual", source)
        assert [r.state for r in rows] == ["posted"] and rows[0].posted_at is not None
        assert rows[0].amount == Decimal("800.00")
        with pytest.raises(ValidationFailed):
            await postings.release(ctx, db, "billing", reason="  ", document_id=doc)
        assert await postings.release(ctx, db, "billing", reason="credit note", document_id=doc) == 1
        assert not await postings.has_active_posting(db, "manual", source)
        again = await postings.reserve(ctx, db, "billing", [_line(source, document_id=uuid.uuid4())])
        assert len(again) == 1
        target = rows[0].target_id
        assert [s.source_id for s in await postings.sources_for(db, "billing", "invoice_line", target)] == [
            source
        ]
        active = await postings.active_postings_for(db, "manual", [source, uuid.uuid4()])
        assert [p.id for p in active[source]] == again


async def test_second_active_posting_conflicts_and_session_stays_usable() -> None:
    tenant_id = await _tenant()
    ctx = _ctx(tenant_id)
    source = uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        await postings.reserve(ctx, db, "billing", [_line(source)])
        with pytest.raises(ConflictError) as exc:
            await postings.reserve(ctx, db, "billing", [_line(source)])
        assert exc.value.code == "source_already_posted"
        assert exc.value.details["source_ids"] == [str(source)]
        # the outer transaction is still usable
        other = await postings.record_posted(
            ctx,
            db,
            "payable",
            [
                PostingLine(
                    source_kind="manual",
                    source_id=source,
                    target_kind="payroll_row",
                    target_id=uuid.uuid4(),
                    document_id=None,
                    quantity=Decimal(1),
                    amount=Decimal("10.00"),
                    currency="INR",
                    payee_kind="worker",
                )
            ],
        )
        assert len(other) == 1


async def test_concurrent_reserve_one_wins() -> None:
    tenant_id = await _tenant()
    source = uuid.uuid4()
    gate = asyncio.Event()

    async def attempt() -> str:
        try:
            async with tenant_session(tenant_id) as db:
                await postings.reserve(_ctx(tenant_id), db, "billing", [_line(source)])
                await gate.wait()
            return "ok"
        except ConflictError as exc:
            return exc.code

    first = asyncio.create_task(attempt())
    await asyncio.sleep(0.3)
    second = asyncio.create_task(attempt())
    await asyncio.sleep(0.3)
    gate.set()
    results = sorted(await asyncio.gather(first, second))
    assert results == ["ok", "source_already_posted"]


async def test_unknown_source_kind_and_wrong_ledger_refused() -> None:
    tenant_id = await _tenant()
    ctx = _ctx(tenant_id)
    with _registered("p13_billing_only", None, frozenset({"billing"})):
        async with tenant_session(tenant_id) as db:
            with pytest.raises(ValidationFailed) as exc:
                await postings.reserve(ctx, db, "billing", [_line(uuid.uuid4(), kind="foo")])
            assert exc.value.code == "unknown_posting_source"
            line = PostingLine(
                source_kind="p13_billing_only",
                source_id=uuid.uuid4(),
                target_kind="payroll_row",
                target_id=uuid.uuid4(),
                document_id=None,
                quantity=Decimal(1),
                amount=Decimal(1),
                currency="INR",
                payee_kind="worker",
            )
            with pytest.raises(ValidationFailed) as wrong:
                await postings.record_posted(ctx, db, "payable", [line])
            assert wrong.value.code == "unknown_posting_source"
            with pytest.raises(TypeError):
                await postings.reserve(
                    ctx,
                    db,
                    "billing",
                    [
                        PostingLine(
                            source_kind="manual",
                            source_id=uuid.uuid4(),
                            target_kind="invoice_line",
                            target_id=uuid.uuid4(),
                            document_id=None,
                            quantity=Decimal(1),
                            amount=1.5,  # type: ignore[arg-type]
                            currency="INR",
                        )
                    ],
                )


async def test_ledger_rows_immutable_except_state() -> None:
    tenant_id = await _tenant()
    ctx = _ctx(tenant_id)
    async with tenant_session(tenant_id) as db:
        (pid,) = await postings.reserve(ctx, db, "billing", [_line(uuid.uuid4())])
    async with tenant_session(tenant_id) as db:
        with pytest.raises(DBAPIError) as exc:
            await db.execute(text("UPDATE billing_postings SET amount = 1 WHERE id = :i"), {"i": pid})
        assert sqlstate(exc.value) == "TOS02"
    async with tenant_session(tenant_id) as db:
        with pytest.raises(DBAPIError) as exc:
            await db.execute(text("DELETE FROM billing_postings WHERE id = :i"), {"i": pid})
        assert sqlstate(exc.value) is not None
    async with tenant_session(tenant_id) as db:
        assert await postings.post(ctx, db, "billing", posting_ids=[pid]) == 1
    async with tenant_session(tenant_id) as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE billing_postings SET state = 'reserved', posted_at = NULL WHERE id = :i"),
                {"i": pid},
            )
    async with tenant_session(tenant_id) as db:
        assert await postings.release(ctx, db, "billing", reason="discard", posting_ids=[pid]) == 1
    async with tenant_session(tenant_id) as db:
        with pytest.raises(DBAPIError):
            await db.execute(text("UPDATE billing_postings SET state = 'posted' WHERE id = :i"), {"i": pid})


class _registered:
    def __init__(
        self, kind: str, loader: postings.SourceLoader | None, ledgers: frozenset[postings.Ledger]
    ) -> None:
        self.kind, self.loader, self.ledgers = kind, loader, ledgers
        self._cm: Iterator[None] | None = None

    def __enter__(self) -> None:
        from app.services.postings import _SourceDef, _sources

        self._cm = _sources.override(self.kind, _SourceDef(self.loader, self.ledgers))  # type: ignore[assignment]
        self._cm.__enter__()  # type: ignore[union-attr]

    def __exit__(self, *exc: object) -> None:
        self._cm.__exit__(*exc)  # type: ignore[union-attr]


async def test_load_unposted_excludes_active_postings() -> None:
    tenant_id = await _tenant()
    ctx = _ctx(tenant_id)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seen: list[SourceQuery] = []

    async def loader(_ctx: RequestContext, _db: object, query: SourceQuery) -> list[SourceItem]:
        seen.append(query)
        return [
            SourceItem(
                "p13_entry", sid, None, date(2026, 9, 1), Decimal(8), Decimal(80), "INR", {"billable": True}
            )
            for sid in (a, b, c)
        ]

    with _registered("p13_entry", loader, frozenset({"billing"})):
        async with tenant_session(tenant_id) as db:
            await postings.reserve(ctx, db, "billing", [_line(a, kind="p13_entry")])
            (released,) = await postings.reserve(ctx, db, "billing", [_line(b, kind="p13_entry")])
            await postings.release(ctx, db, "billing", reason="discard", posting_ids=[released])
            items = await postings.load_unposted(
                ctx, db, SourceQuery(ledger="billing", period_end=date(2026, 9, 30))
            )
            assert sorted(i.source_id for i in items) == sorted([b, c])
            assert seen[0].ledger == "billing"
            assert (
                await postings.load_unposted(
                    ctx, db, SourceQuery(ledger="payable", period_end=date(2026, 9, 30))
                )
                == []
            )
    assert "p13_entry" not in postings.posting_sources()

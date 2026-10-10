# ruff: noqa: E501
"""Epic 51 (FR-254): 26AS / AIS TDS reconciliation -- pure parsers and matcher, runs, human-only decisions,
supersede carry-over, exception lists, status, TAN mappings, and NO write to ``tds_deductions`` /
``tds_deduction_reversals``."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.core.permissions import CATALOG, Principal, Scope
from app.models.receivables import TdsDeduction, TdsDeductionReversal
from app.services.receivables.tds_recon import matching, parser_26as, parser_ais, runs, status
from app.services.receivables.tds_recon.common import StatementError, fy_from_assessment_year, hashes
from tests.modules.receivables.conftest import R, ROrg, err, invoice, ok

D = Decimal
PAN = "ABCDE1234F"
TAN_A = "BLRA12345B"
TAN_B = "MUMB54321C"
TAN_X = "DELX99999Z"


def statement_26as(pan: str = PAN, ay: str = "2027-28", extra_rows: str = "") -> str:
    """Anonymised, real-shaped TRACES 26AS text export (Part A = TDS)."""
    return (
        "File Creation Date^Permanent Account Number (PAN)^Current Status of PAN^Financial Year^Assessment Year^Name of Assessee\n"
        f"06-10-2026^{pan}^Active^2026-27^{ay}^TALENT STAFFING PVT LTD\n"
        "\n"
        "PART-I - Details of Tax Deducted at Source\n"
        "Sr. No.^Name of Deductor^TAN of Deductor^^^^^Total Amount Paid / Credited(Rs.)^Total Tax Deducted(Rs.)^Total TDS Deposited(Rs.)\n"
        f"1^ACME CORP LTD^{TAN_A}^^^^^3,00,000.00^30,000.00^30,000.00\n"
        "^Sr. No.^Section^Transaction Date^Status of Booking^Date of Booking^Remarks^Amount Paid / Credited(Rs.)^Tax Deducted(Rs.)^TDS Deposited(Rs.)\n"
        "^1^194J^15-Jul-2026^F^31-Oct-2026^-^1,00,000.00^10,000.00^10,000.00\n"
        "^2^194J^20-Aug-2026^U^31-Oct-2026^-^2,00,000.00^20,000.00^20,000.00\n"
        "^3^194J^05-Apr-2026^F^31-Jul-2026^-^50,000.00^5,000.00^5,000.00\n"
        f"2^BETA SERVICES LLP^{TAN_X}^^^^^40,000.00^4,000.00^4,000.00\n"
        "^1^194C^10-Sep-2026^F^31-Oct-2026^-^40,000.00^4,000.00^4,000.00\n"
        f"{extra_rows}"
        "PART-II - Details of Tax Deducted at Source for 15G / 15H\n"
        "Sr. No.^Name of Deductor^TAN of Deductor^^^^^Total Amount Paid / Credited(Rs.)^Total Tax Deducted(Rs.)^Total TDS Deposited(Rs.)\n"
        "1^SHOULD NOT PARSE^ZZZZ00000Z^^^^^1.00^0.00^0.00\n"
        "^1^194A^10-Sep-2026^F^31-Oct-2026^-^1.00^0.00^0.00\n"
    )


# ---------------------------------------------------------------- pure
def test_parser_26as_real_shaped_fixture() -> None:
    parsed = parser_26as.parse(statement_26as(extra_rows="^4^194J^31-Foo-2026^F^-^-^1.00^1.00^1.00\n"))
    assert parsed.pan == PAN and parsed.assessment_year == "2027-28"
    assert fy_from_assessment_year(parsed.assessment_year) == "2026-27"
    assert [
        (ln.deductor_tan, ln.section, ln.transaction_date, ln.tds_deducted, ln.booking_status)
        for ln in parsed.lines
    ] == [
        (TAN_A, "194J", date(2026, 7, 15), D("10000.00"), "F"),
        (TAN_A, "194J", date(2026, 8, 20), D("20000.00"), "U"),
        (TAN_A, "194J", date(2026, 4, 5), D("5000.00"), "F"),
        (TAN_X, "194C", date(2026, 9, 10), D("4000.00"), "F"),
    ]
    assert parsed.lines[0].amount_paid == D("100000.00") and parsed.lines[0].deductor_name == "ACME CORP LTD"
    assert len(parsed.errors) == 1 and "transaction date" in parsed.errors[0].message
    assert len(set(hashes(parsed.lines))) == 4
    with pytest.raises(StatementError) as exc:
        parser_26as.parse("just,a,csv\n1,2,3\n")
    assert exc.value.code == "tds_statement_unreadable"


def test_parser_ais_csv_header_map() -> None:
    csv_text = (
        "PAN,ABCDE1234F\n"
        "Assessment Year,2027-28\n"
        "\n"
        "Sr. No.,Information Source,Section,Date of payment/credit,Amount paid/credited,TDS Deducted,TDS Deposited,Status\n"
        '1,ACME CORP LTD (BLRA12345B),194J,15/07/2026,"1,00,000.00","10,000.00","10,000.00",Active\n'
        "2,NO TAN HERE,194J,15/07/2026,100.00,10.00,10.00,Active\n"
    )
    parsed = parser_ais.parse(csv_text)
    assert parsed.pan == PAN and parsed.assessment_year == "2027-28"
    assert (
        len(parsed.lines) == 1
        and parsed.lines[0].deductor_tan == TAN_A
        and parsed.lines[0].tds_deducted == D("10000.00")
    )
    assert parsed.lines[0].deductor_name == "ACME CORP LTD"
    assert len(parsed.errors) == 1 and "TAN" in parsed.errors[0].message


def test_matching_score_table_and_determinism() -> None:
    client = uuid.uuid4()
    line = matching.LineIn(uuid.UUID(int=1), client, "194J", date(2026, 7, 15), D("10000.00"))
    exact = matching.DeductionIn(uuid.UUID(int=10), client, "194 J", D("10000.00"), date(2026, 7, 20))
    near = matching.DeductionIn(uuid.UUID(int=11), client, "194J", D("10000.80"), date(2026, 7, 20))
    late = matching.DeductionIn(uuid.UUID(int=12), client, "194J", D("10000.00"), date(2026, 10, 20))
    other_section = matching.DeductionIn(uuid.UUID(int=13), client, "194C", D("10000.00"), date(2026, 7, 16))
    assert matching.score(line, exact).value == 100 and matching.score(line, exact).reasons == (
        "section_match",
        "amount_exact",
        "date_within_45_days",
        "same_quarter",
    )
    assert matching.score(line, near).value == 100  # 40 + 40 + 15 + 5
    assert matching.score(line, late).value == 85  # 40 + 45, outside window and quarter
    assert matching.score(line, other_section).value == 65  # below threshold
    line2 = matching.LineIn(uuid.UUID(int=2), client, "194J", date(2026, 7, 21), D("10000.00"))
    unmapped = matching.LineIn(uuid.UUID(int=3), None, "194J", date(2026, 7, 15), D("10000.00"))
    first = matching.propose([line, line2, unmapped], [late, near, exact, other_section])
    again = matching.propose([unmapped, line2, line], [exact, other_section, near, late])
    assert first == again
    by_line = {p.line_id: p.deduction_id for p in first}
    assert set(by_line) == {line.id, line2.id}  # unmapped never proposed
    assert len(set(by_line.values())) == 2  # each deduction used once
    assert status.classify(False, 1, 0, 0, 0) == "not_started"
    assert status.classify(True, 2, 2, 0, 2) == "reconciled"
    assert status.classify(True, 2, 1, 1, 2) == "partially_reconciled"
    assert status.classify(True, 2, 0, 2, 2) == "exceptions"


# ---------------------------------------------------------------- API
async def _setup(org: ROrg) -> dict[str, Any]:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE legal_entities SET pan = :p WHERE id = :i"), {"p": PAN, "i": org.entity_id}
        )
    await ok(
        await org.c.post(
            f"{R}/tds-recon/deductor-tans",
            json={"client_id": str(org.client_id), "tan": TAN_A.lower()},
            headers=org.h(),
        ),
        201,
    )
    deds = {}
    for key, amount, on, section in (
        ("jul", "10000.00", date(2026, 7, 18), "194J"),
        ("aug", "20000.00", date(2026, 8, 22), "194J"),
        ("sep", "7000.00", date(2026, 9, 25), "194J"),
    ):
        inv = await invoice(org, "100000.00", doc_date=on)
        did = uuid.uuid4()
        async with tenant_session(org.tenant_id) as db:
            db.add(TdsDeduction(
                id=did, tenant_id=org.tenant_id, legal_entity_id=org.entity_id, client_id=org.client_id, invoice_id=inv, kind="income_tax",
                section_code=section, rate_pct=D("10"), base_amount=D("100000.00"), expected_amount=D(amount), amount=D(amount),
                currency="INR", inr_amount=D(amount), deduction_date=on, quarter="26-27 Q2",
            ))  # fmt: skip
        deds[key] = did
    return deds


async def _deduction_fingerprint(org: ROrg) -> tuple[Any, ...]:
    async with tenant_session(org.tenant_id) as db:
        n = (await db.execute(select(func.count(TdsDeduction.id)))).scalar()
        latest = (await db.execute(select(func.max(TdsDeduction.updated_at)))).scalar()
        rev = (await db.execute(select(func.count(TdsDeductionReversal.id)))).scalar()
    return n, latest, rev


async def _upload(org: ROrg, content: str, quarter: str = "Q2", kind: str = "26as") -> Any:
    return await org.c.post(
        f"{R}/tds-recon/runs",
        data={
            "legal_entity_id": str(org.entity_id),
            "fy": "2026-27",
            "quarter": quarter,
            "statement_kind": kind,
        },
        files={"file": ("26as.txt", content.encode(), "text/plain")},
        headers=org.h(),
    )


def _api_key_ctx(org: ROrg) -> RequestContext:
    principal = Principal(
        type="api_key",
        id=uuid.uuid4(),
        tenant_id=org.tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=org.tenant_id, principal=principal)


async def test_run_proposals_human_decisions_exceptions_and_status(rorg: ROrg, other_rorg: ROrg) -> None:
    deds = await _setup(rorg)
    before = await _deduction_fingerprint(rorg)
    err(await _upload(rorg, statement_26as(pan="ZZZZZ9999Z")), 422, "tds_statement_pan_mismatch")
    err(await _upload(rorg, statement_26as(ay="2026-27")), 422, "tds_statement_fy_mismatch")
    run = await ok(await _upload(rorg, statement_26as()), 202)
    detail = await ok(await rorg.c.get(f"{R}/tds-recon/runs/{run['id']}"))
    assert detail["run"]["status"] == "ready" and detail["run"]["line_count"] == 3  # the April line is Q1
    lines = {ln["deductor_tan"] + ln["transaction_date"]: ln for ln in detail["lines"]}
    jul, aug, beta = lines[TAN_A + "2026-07-15"], lines[TAN_A + "2026-08-20"], lines[TAN_X + "2026-09-10"]
    assert (
        jul["match"]["status"] == "proposed"
        and jul["match"]["tds_deduction_id"] == str(deds["jul"])
        and jul["match"]["score"] == 100
    )
    assert aug["match"]["tds_deduction_id"] == str(deds["aug"]) and "amount_exact" in aug["match"]["reasons"]
    assert beta["match"] is None and beta["client_id"] is None  # unmapped TAN: never proposed
    # human-only: an API key / workflow cannot confirm
    async with tenant_session(rorg.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await runs.confirm(_api_key_ctx(rorg), db, uuid.UUID(jul["match"]["id"]))
        assert exc.value.code == "human_only_action"
    confirmed = await ok(
        await rorg.c.post(f"{R}/tds-recon/matches/{jul['match']['id']}/confirm", json={}, headers=rorg.h())
    )
    assert confirmed["status"] == "confirmed" and confirmed["decided_by"] is not None
    await ok(await rorg.c.post(f"{R}/tds-recon/matches/{aug['match']['id']}/confirm", headers=rorg.h()))
    err(
        await rorg.c.post(f"{R}/tds-recon/matches/{aug['match']['id']}/confirm", headers=rorg.h()),
        409,
        "invalid_state",
    )
    # exceptions: the unmapped line is deducted-not-recorded; Sep deduction has no line; Aug line booked U
    exc_out = await ok(
        await rorg.c.get(
            f"{R}/tds-recon/exceptions",
            params={"legal_entity_id": str(rorg.entity_id), "fy": "2026-27", "quarter": "Q2"},
        )
    )
    assert [ln["deductor_tan"] for ln in exc_out["deducted_not_recorded"]] == [TAN_X]
    reasons = {r["id"]: r["reason"] for r in exc_out["recorded_not_deposited"]}
    assert reasons == {str(deds["sep"]): "no_statement_line", str(deds["aug"]): "booking_unmatched"}
    st = await ok(await rorg.c.get(f"{R}/tds-recon/status", params={"fy": "2026-27"}))
    row = next(r for r in st if r["client_id"] == str(rorg.client_id) and r["quarter"] == "Q2")
    assert (
        row["status"] == "partially_reconciled" and row["recorded_count"] == 3 and row["matched_count"] == 2
    )
    # acknowledge the unknown deductor line; manual pairing of a candidate
    ack = await ok(
        await rorg.c.post(
            f"{R}/tds-recon/lines/{beta['id']}/acknowledge",
            json={"note": "Interest, not a client"},
            headers=rorg.h(),
        )
    )
    assert ack["status"] == "unmatched_ack"
    cands = await ok(await rorg.c.get(f"{R}/tds-recon/lines/{jul['id']}/candidates"))
    assert str(deds["sep"]) in [c["id"] for c in cands] and str(deds["jul"]) not in [c["id"] for c in cands]
    # cross-tenant
    assert (await other_rorg.c.get(f"{R}/tds-recon/runs/{run['id']}")).status_code == 404
    assert (
        await other_rorg.c.post(f"{R}/tds-recon/matches/{jul['match']['id']}/reject", headers=other_rorg.h())
    ).status_code == 404
    # supersede: the same statement again carries confirmed / acknowledged decisions over by line hash
    run2 = await ok(await _upload(rorg, statement_26as()), 202)
    runs_list = await ok(await rorg.c.get(f"{R}/tds-recon/runs", params={"fy": "2026-27", "quarter": "Q2"}))
    assert {r["id"]: r["status"] for r in runs_list} == {run["id"]: "superseded", run2["id"]: "ready"}
    detail2 = await ok(await rorg.c.get(f"{R}/tds-recon/runs/{run2['id']}"))
    carried = {ln["deductor_tan"] + ln["transaction_date"]: ln["match"]["status"] for ln in detail2["lines"]}
    assert carried == {
        TAN_A + "2026-07-15": "confirmed",
        TAN_A + "2026-08-20": "confirmed",
        TAN_X + "2026-09-10": "unmatched_ack",
    }
    err(
        await rorg.c.post(
            f"{R}/tds-recon/lines/{beta['id']}/acknowledge", json={"note": "x"}, headers=rorg.h()
        ),
        409,
        "tds_run_not_ready",
    )
    # reconciliation never created or changed a TDS deduction / reversal
    assert await _deduction_fingerprint(rorg) == before


async def test_unmapped_tan_proposed_after_mapping_and_manual_pair(rorg: ROrg) -> None:
    deds = await _setup(rorg)
    run = await ok(await _upload(rorg, statement_26as()), 202)
    detail = await ok(await rorg.c.get(f"{R}/tds-recon/runs/{run['id']}"))
    beta = next(ln for ln in detail["lines"] if ln["deductor_tan"] == TAN_X)
    # manual pairing with "remember the TAN" records the mapping
    m = await ok(
        await rorg.c.post(
            f"{R}/tds-recon/lines/{beta['id']}/match",
            json={"tds_deduction_id": str(deds["sep"]), "record_tan_mapping": True},
            headers=rorg.h(),
        )
    )
    assert m["status"] == "confirmed" and "manual_pair" in m["reasons"]
    tans = await ok(await rorg.c.get(f"{R}/tds-recon/deductor-tans"))
    assert sorted(t["tan"] for t in tans) == sorted([TAN_A, TAN_X])
    err(
        await rorg.c.post(
            f"{R}/tds-recon/deductor-tans",
            json={"client_id": str(rorg.client_id), "tan": TAN_X},
            headers=rorg.h(),
        ),
        409,
        "tan_already_mapped",
    )
    err(
        await rorg.c.post(
            f"{R}/tds-recon/deductor-tans",
            json={"client_id": str(rorg.client_id), "tan": "BAD"},
            headers=rorg.h(),
        ),
        422,
        "validation_failed",
    )
    # a deduction already confirmed elsewhere cannot be paired again
    other = next(ln for ln in detail["lines"] if ln["transaction_date"] == "2026-07-15")
    await ok(await rorg.c.post(f"{R}/tds-recon/matches/{other['match']['id']}/reject", headers=rorg.h()))
    err(
        await rorg.c.post(
            f"{R}/tds-recon/lines/{other['id']}/match",
            json={"tds_deduction_id": str(deds["sep"])},
            headers=rorg.h(),
        ),
        409,
        "tds_match_conflict",
    )
    # delete a mapping; a new mapping makes open lines proposable again
    tan_row = next(t for t in tans if t["tan"] == TAN_A)
    assert (
        await rorg.c.delete(f"{R}/tds-recon/deductor-tans/{tan_row['id']}", headers=rorg.h())
    ).status_code == 204


async def test_upload_without_file_needs_a_connector(rorg: ROrg) -> None:
    await _setup(rorg)
    resp = await rorg.c.post(
        f"{R}/tds-recon/runs",
        data={
            "legal_entity_id": str(rorg.entity_id),
            "fy": "2026-27",
            "quarter": "Q2",
            "statement_kind": "26as",
        },
        headers=rorg.h(),
    )
    err(resp, 422, "tds_statement_file_required")
    from app.models.connectors import ProviderConnectorConfig

    async with tenant_session(rorg.tenant_id) as db:
        db.add(
            ProviderConnectorConfig(
                tenant_id=rorg.tenant_id,
                category="tds_statement",
                provider_key="fake",
                display_name="Fake",
                settings={},
                status="active",
            )
        )
    run = await ok(await rorg.c.post(
        f"{R}/tds-recon/runs",
        data={"legal_entity_id": str(rorg.entity_id), "fy": "2026-27", "quarter": "Q2", "statement_kind": "26as"},
        headers=rorg.h(),
    ), 202)  # fmt: skip
    detail = await ok(await rorg.c.get(f"{R}/tds-recon/runs/{run['id']}"))
    assert (
        detail["run"]["source"] == "connector"
        and detail["run"]["status"] == "ready"
        and detail["run"]["line_count"] >= 1
    )

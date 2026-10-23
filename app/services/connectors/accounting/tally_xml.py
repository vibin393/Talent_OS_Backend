# ruff: file-ignore[line-too-long, non-imperative-mood]
"""TallyPrime XML import-file adapter (Epic 71, spec A7; key ``tally_xml``; coordinator decision D-5: a file export
only, no TallyPrime push). Pure builder: no network, no secrets.

One file per run: an ``ENVELOPE`` with an ``All Masters`` import (party ledgers, optional) followed by a ``Vouchers``
import. Each voucher carries ``REMOTEID="talentos-{document_id}-v{version}"`` so re-importing the same document
version updates instead of duplicating. Amounts are INR with 2 decimals (the ``inr_*`` values of the document);
foreign-currency details go to the narration only (spec Known gap 4). Debits are negative with
``ISDEEMEDPOSITIVE=Yes``; credits positive with ``ISDEEMEDPOSITIVE=No`` (TallyPrime import convention).

* ``push_documents(docs)`` returns ``synced`` with ``external_ref = REMOTEID`` (the file is built by ``export``).
* ``export(rows, template)`` builds the XML bytes: ``rows`` are voucher dicts (``voucher_row(dto)``), ``template``
  holds ``company_name``, voucher types, ``party_group`` and optional ``masters`` (party ledger dicts).

Verified against the recorded TallyPrime import schema fixture ``tests/fixtures/tally/tallyprime_import.xsd``
(TallyPrime release 4.1 / 5.0 import format).
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from pydantic import BaseModel, ConfigDict, Field

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import ManualActionRequired
from app.services.connectors.accounting.port import LedgerDocumentDTO, LedgerPaymentDTO, LedgerResultDTO

__all__ = ["TallyConfig", "TallyXmlAdapter", "build_import_xml", "remote_id", "voucher_row"]

_CENT = Decimal("0.01")


class TallyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(default="", max_length=200)
    sales_voucher_type: str = Field(default="Sales", max_length=60)
    credit_note_voucher_type: str = Field(default="Credit Note", max_length=60)
    debit_note_voucher_type: str = Field(default="Debit Note", max_length=60)
    receipt_voucher_type: str = Field(default="Receipt", max_length=60)
    party_group: str = Field(default="Sundry Debtors", max_length=100)
    include_payments: bool = True


def remote_id(document_id: uuid.UUID | str, version: int) -> str:
    return f"talentos-{document_id}-v{int(version)}"


def _amt(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _fmt(value: Decimal) -> str:
    return f"{value:.2f}"


def _x(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def _tally_date(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y%m%d")
    return str(value or "").replace("-", "")[:8]


def voucher_row(doc: LedgerDocumentDTO) -> dict[str, Any]:
    """The builder row of one document (payload keys documented in ``ledger_sync.documents``)."""
    return {
        "remote_id": remote_id(doc.document_id, doc.document_version),
        "kind": doc.kind,
        "number": doc.number,
        "date": doc.issue_date.isoformat(),
        "party": doc.customer_ref or "",
        **dict(doc.payload),
    }


def _entry(ledger: str, amount: Decimal, *, debit: bool, bill: tuple[str, str] | None = None) -> str:
    signed = -amount if debit else amount
    parts = [
        "<ALLLEDGERENTRIES.LIST>",
        f"<LEDGERNAME>{_x(ledger)}</LEDGERNAME>",
        f"<ISDEEMEDPOSITIVE>{'Yes' if debit else 'No'}</ISDEEMEDPOSITIVE>",
        f"<ISPARTYLEDGER>{'Yes' if bill else 'No'}</ISPARTYLEDGER>",
        f"<AMOUNT>{_fmt(signed)}</AMOUNT>",
    ]
    if bill is not None:
        parts += [
            "<BILLALLOCATIONS.LIST>",
            f"<NAME>{_x(bill[0])}</NAME>",
            f"<BILLTYPE>{_x(bill[1])}</BILLTYPE>",
            f"<AMOUNT>{_fmt(signed)}</AMOUNT>",
            "</BILLALLOCATIONS.LIST>",
        ]
    parts.append("</ALLLEDGERENTRIES.LIST>")
    return "".join(parts)


def _document_entries(row: Mapping[str, Any]) -> list[str]:
    """Invoice / debit note: party Dr, revenue + GST + round-off Cr. Credit note: the mirror image."""
    reverse = row["kind"] == "credit_note"
    entries: list[str] = []
    revenue: dict[str, Decimal] = {}
    taxes: dict[str, Decimal] = {}
    for line in row.get("lines") or []:
        ledger = str(line.get("revenue_ref") or "")
        revenue[ledger] = revenue.get(ledger, Decimal(0)) + _amt(line.get("inr_taxable_value"))
        for tax in line.get("taxes") or []:
            name = str(tax.get("ledger_ref") or "")
            taxes[name] = taxes.get(name, Decimal(0)) + _amt(tax.get("inr_amount"))
    round_off = _amt(row.get("inr_round_off"))
    total = sum(revenue.values(), Decimal(0)) + sum(taxes.values(), Decimal(0)) + round_off
    if reverse:
        bill = (str(row.get("linked_invoice_number") or row["number"]), "Agst Ref")
    else:
        bill = (str(row["number"]), "New Ref")
    entries.append(_entry(str(row.get("party") or ""), total, debit=not reverse, bill=bill))
    for ledger, amount in sorted(revenue.items()):
        entries.append(_entry(ledger, amount, debit=reverse))
    for ledger, amount in sorted(taxes.items()):
        if amount:
            entries.append(_entry(ledger, amount, debit=reverse))
    if round_off:
        name = str(row.get("round_off_ref") or "Round Off")
        if round_off > 0:
            entries.append(_entry(name, round_off, debit=reverse))
        else:
            entries.append(_entry(name, -round_off, debit=not reverse))
    return entries


def _receipt_entries(row: Mapping[str, Any]) -> list[str]:
    """Receipt: bank Dr (net), TDS receivable Dr, party Cr against each invoice."""
    p = row.get("payment") or {}
    amount = _amt(p.get("inr_amount") or p.get("amount"))
    tds = _amt(p.get("inr_tds_amount") or p.get("tds_amount"))
    entries = [_entry(str(p.get("bank_ref") or ""), amount, debit=True)]
    if tds:
        entries.append(_entry(str(p.get("tds_ref") or ""), tds, debit=True))
    allocations = p.get("allocations") or []
    if allocations:
        for alloc in allocations:
            entries.append(
                _entry(str(row.get("party") or ""), _amt(alloc.get("inr_amount") or alloc.get("amount")) + _amt(alloc.get("inr_tds") or alloc.get("tds")), debit=False, bill=(str(alloc.get("invoice_number") or ""), "Agst Ref"))
            )
    else:
        entries.append(_entry(str(row.get("party") or ""), amount + tds, debit=False, bill=(str(row["number"]), "On Account")))
    return entries


def _voucher(row: Mapping[str, Any], cfg: TallyConfig) -> str:
    vtype = {
        "invoice": cfg.sales_voucher_type,
        "debit_note": cfg.debit_note_voucher_type,
        "credit_note": cfg.credit_note_voucher_type,
        "payment": cfg.receipt_voucher_type,
    }[str(row["kind"])]
    narration = str(row.get("narration") or "")
    if row.get("irn"):
        narration = f"IRN {row['irn']}" + (f"; Ack {row['ack_no']}" if row.get("ack_no") else "") + (f"; {narration}" if narration else "")
    currency = str(row.get("currency") or "INR")
    if currency != "INR":
        narration = (narration + "; " if narration else "") + f"{currency} {_fmt(_amt(row.get('total')))} @ {row.get('exchange_rate') or ''}".strip()
    is_invoice = row["kind"] != "payment"
    view = "Invoice Voucher View" if is_invoice else "Accounting Voucher View"
    head = (
        f"<VOUCHER REMOTEID={quoteattr(str(row['remote_id']))} VCHTYPE={quoteattr(vtype)} ACTION=\"Create\" OBJVIEW={quoteattr(view)}>"
        f"<DATE>{_tally_date(row['date'])}</DATE>"
        f"<VOUCHERTYPENAME>{_x(vtype)}</VOUCHERTYPENAME>"
        f"<VOUCHERNUMBER>{_x(row['number'])}</VOUCHERNUMBER>"
        f"<PARTYLEDGERNAME>{_x(row.get('party'))}</PARTYLEDGERNAME>"
    )
    if is_invoice and row.get("place_of_supply_name"):
        head += f"<PLACEOFSUPPLY>{_x(row['place_of_supply_name'])}</PLACEOFSUPPLY>"
    if row.get("reference_number"):
        head += f"<REFERENCE>{_x(row['reference_number'])}</REFERENCE>"
    head += f"<NARRATION>{_x(narration)}</NARRATION>"
    head += f"<ISINVOICE>{'Yes' if is_invoice else 'No'}</ISINVOICE>"
    body = "".join(_document_entries(row) if is_invoice else _receipt_entries(row))
    return f"{head}{body}</VOUCHER>"


def _ledger_master(m: Mapping[str, Any], cfg: TallyConfig) -> str:
    name = str(m.get("name") or "")
    gstin = str(m.get("gstin") or "")
    reg = "Regular" if gstin else ("Consumer" if m.get("consumer") else "Unregistered")
    parts = [
        f"<LEDGER NAME={quoteattr(name)} ACTION=\"Create\">",
        f"<NAME.LIST><NAME>{_x(name)}</NAME></NAME.LIST>",
        f"<PARENT>{_x(cfg.party_group)}</PARENT>",
        f"<GSTREGISTRATIONTYPE>{reg}</GSTREGISTRATIONTYPE>",
    ]
    if gstin:
        parts.append(f"<PARTYGSTIN>{_x(gstin)}</PARTYGSTIN>")
    if m.get("state"):
        parts.append(f"<LEDSTATENAME>{_x(m['state'])}</LEDSTATENAME>")
    parts.append(f"<COUNTRYNAME>{_x(m.get('country') or 'India')}</COUNTRYNAME>")
    parts.append("<ISBILLWISEON>Yes</ISBILLWISEON></LEDGER>")
    return "".join(parts)


def _import(report: str, company: str, messages: Sequence[str]) -> str:
    body = "".join(f"<TALLYMESSAGE xmlns:UDF=\"TallyUDF\">{m}</TALLYMESSAGE>" for m in messages)
    return (
        "<IMPORTDATA><REQUESTDESC>"
        f"<REPORTNAME>{report}</REPORTNAME>"
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{_x(company)}</SVCURRENTCOMPANY></STATICVARIABLES>"
        f"</REQUESTDESC><REQUESTDATA>{body}</REQUESTDATA></IMPORTDATA>"
    )


def build_import_xml(cfg: TallyConfig, vouchers: Sequence[Mapping[str, Any]], masters: Sequence[Mapping[str, Any]] = ()) -> bytes:
    """The import file (UTF-8). Masters first (when any), then vouchers; deterministic for the same input."""
    company = cfg.company_name
    imports = []
    if masters:
        imports.append(_import("All Masters", company, [_ledger_master(m, cfg) for m in sorted(masters, key=lambda m: str(m.get("name")))]))
    rows = [v for v in vouchers if v["kind"] != "payment" or cfg.include_payments]
    imports.append(_import("Vouchers", company, [_voucher(v, cfg) for v in rows]))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>'
        f"<BODY>{''.join(imports)}</BODY></ENVELOPE>\n"
    )
    return xml.encode("utf-8")


class TallyXmlAdapter:
    """Builds files; no network. ``pull_payments`` is not supported (Tally users reconcile manually, Known gap 5)."""

    category = "accounting"
    provider_key = "tally_xml"

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.key = self.provider_key

    def __repr__(self) -> str:
        return f"TallyXmlAdapter(mode={self.ctx.mode})"

    @property
    def _cfg(self) -> TallyConfig:
        cfg = self.ctx.config
        return cfg if isinstance(cfg, TallyConfig) else TallyConfig()

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        if op == "health":
            return {"ok": True}
        raise ManualActionRequired("Generate a Tally import file from Ledger sync", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def export(self, rows: Sequence[Mapping[str, Any]], template: Mapping[str, Any]) -> bytes:
        cfg = self._cfg
        if template.get("company_name"):
            cfg = cfg.model_copy(update={"company_name": str(template["company_name"])})
        return build_import_xml(cfg, rows, template.get("masters") or ())

    async def push_documents(self, docs: Sequence[LedgerDocumentDTO]) -> Sequence[LedgerResultDTO]:
        return [
            LedgerResultDTO(document_id=d.document_id, external_ref=remote_id(d.document_id, d.document_version), status="synced", message=None)
            for d in docs
        ]

    async def document_status(self, refs: Sequence[str]) -> Sequence[LedgerResultDTO]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired("TallyPrime import results are not reported back; check the import log in Tally", {"op": "document_status"})

    async def pull_payments(self, since: datetime | None, cursor: str | None) -> tuple[Sequence[LedgerPaymentDTO], str | None]:  # ruff: ignore[unused-method-argument]
        return (), None

    async def masters_sync(self, customers: Sequence[Mapping[str, Any]], ledgers: Sequence[Mapping[str, Any]]) -> Mapping[str, str]:  # ruff: ignore[unused-method-argument]
        """Tally party ledgers are referenced by name: the mapping is the client name itself."""
        return {str(c.get("key") or c.get("name")): str(c.get("name")) for c in customers if c.get("name")}

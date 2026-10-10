"""Go-live CSV templates and column dictionaries (Epic 35 A8). Money = decimal strings, dates ``yyyy-mm-dd``,
UTF-8 with or without BOM."""

import csv
import dataclasses
import io

from app.core.errors import NotFound


@dataclasses.dataclass(frozen=True, slots=True)
class Column:
    name: str
    required: bool
    description: str
    example: str
    sensitive: bool = False  # bank / identity: never echoed back (error CSV shows ****1234)


@dataclasses.dataclass(frozen=True, slots=True)
class KindDef:
    kind: str
    label: str
    owner_epic: str | None
    depends_on: tuple[str, ...]
    columns: tuple[Column, ...]

    @property
    def headers(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def required(self) -> list[str]:
        return [c.name for c in self.columns if c.required]

    @property
    def sensitive(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns if c.sensitive)


_LE = Column("legal_entity_id", False, "Legal Entity id (blank = the tenant's default entity)", "")

KINDS: dict[str, KindDef] = {
    "workers": KindDef(
        "workers",
        "Workers",
        None,
        (),
        (
            Column("full_name", True, "Worker's full name", "Asha Rao"),
            Column(
                "email", False, "Email (matched first; email or phone is required)", "asha.rao@example.com"
            ),
            Column("phone", False, "Mobile number (matched when no email matches)", "+919876543210"),
            Column("current_title", False, "Current job title", "Data Engineer"),
            Column("current_employer", False, "Current employer", "Acme Pvt Ltd"),
            Column("pan", False, "PAN (stored encrypted)", "ABCDE1234F", sensitive=True),
            Column(
                "bank_account_number",
                False,
                "Bank account number (stored encrypted)",
                "123456789012",
                sensitive=True,
            ),
            Column("ifsc", False, "Bank IFSC (required with an account number)", "HDFC0001234"),
            Column("bank_holder_name", False, "Account holder name (blank = full name)", "Asha Rao"),
        ),
    ),
    "placements": KindDef(
        "placements",
        "Active Placements",
        "Epic 19",
        ("workers",),
        (
            Column("worker_email", True, "Email of an imported / existing Worker", "asha.rao@example.com"),
            Column("client_id", True, "Client id", "00000000-0000-0000-0000-000000000000"),
            _LE,
            Column("worker_type", True, "employee | contractor | vendor_contractor", "employee"),
            Column("engagement_kind", False, "contract | permanent (blank = contract)", "contract"),
            Column("start_date", True, "Actual start date (yyyy-mm-dd)", "2026-04-01"),
            Column("end_date", False, "Planned end date (yyyy-mm-dd)", "2027-03-31"),
            Column("currency", False, "ISO currency (blank = INR)", "INR"),
            Column("rate_unit", True, "hour | day | month | fixed", "hour"),
            Column("pay_rate", False, "Pay rate (decimal string)", "1000.00"),
            Column("bill_rate", False, "Bill rate (decimal string)", "1500.00"),
            Column("vendor_rate", False, "Vendor rate (decimal string)", ""),
            Column("burden_pct", False, "Burden percent (decimal string)", "10"),
            Column("vendor_id", False, "Vendor id (vendor-supplied Workers)", ""),
            Column("contract_id", False, "Contract id", ""),
            Column("po_id", False, "Purchase Order id", ""),
            Column("work_state_code", False, "Work state code (GST place of supply)", "TN"),
        ),
    ),
    "opening_invoices": KindDef(
        "opening_invoices",
        "Opening Invoices",
        "Epic 29",
        (),
        (
            Column("number", True, "Original invoice number (kept as is)", "INV/25-26/0042"),
            Column("doc_date", True, "Invoice date (yyyy-mm-dd)", "2026-03-15"),
            Column("due_date", False, "Due date (yyyy-mm-dd)", "2026-04-14"),
            Column("client_id", True, "Client id", "00000000-0000-0000-0000-000000000000"),
            _LE,
            Column("gst_registration_id", False, "Supplier GST registration id (blank = default)", ""),
            Column("currency", False, "ISO currency (blank = INR)", "INR"),
            Column("taxable_total", True, "Taxable value (decimal string)", "100000.00"),
            Column("tax_total", False, "Tax charged (decimal string)", "18000.00"),
            Column("document_total", True, "Invoice total (decimal string)", "118000.00"),
            Column("fx_rate", False, "FX rate to INR (foreign currency only)", ""),
            Column(
                "sequence", False, "Counter value this number represents (raises the series counter)", "42"
            ),
        ),
    ),
    "leave_balances": KindDef(
        "leave_balances",
        "Opening Leave Balances",
        "Epic 26",
        ("workers",),
        (
            Column("worker_email", True, "Email of an imported / existing Worker", "asha.rao@example.com"),
            Column("leave_type_code", True, "Leave type code", "CL"),
            _LE,
            Column("days", True, "Opening balance in days (decimal string, up to 2 decimals)", "7.5"),
            Column("leave_year", False, "Leave year (blank = current)", ""),
        ),
    ),
}


def kind_def(kind: str) -> KindDef:
    found = KINDS.get(kind)
    if found is None:
        raise NotFound("Unknown go-live import kind", code="unknown_kind")
    return found


def template_csv(kind: str) -> str:
    k = kind_def(kind)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(k.headers)
    writer.writerow([c.example for c in k.columns])
    return buf.getvalue()


def dictionary(kind: str) -> list[dict[str, object]]:
    return [
        {"column": c.name, "required": c.required, "description": c.description, "example": c.example}
        for c in kind_def(kind).columns
    ]

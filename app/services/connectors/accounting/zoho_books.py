# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Zoho Books adapter (Epic 71, spec A7; key ``zoho_books``; India data centre). Live mode only; tests use recorded
fixtures (``httpx.MockTransport``), never the network.

* Base ``https://www.zohoapis.in/books/v3``; every call carries ``organization_id``. Access tokens are minted from the
  vault refresh token (``POST https://accounts.zoho.in/oauth/v2/token`` grant ``refresh_token``) and cached per adapter.
* Zoho answers ``{"code": 0, ...}``; ``code != 0`` -> ``ConnectorPermanentError("zoho_<code>")``; 401 -> auth; 429 ->
  rate limited (100 requests / min per organisation + the plan's daily quota); 5xx -> transient.
* ``push_documents``: invoice -> ``POST /invoices?ignore_auto_number_generation=true`` + ``POST /invoices/{id}/status/sent``;
  update -> ``PUT /invoices/{id}``; void -> ``POST /invoices/{id}/status/void``; credit note ->
  ``POST /creditnotes?ignore_auto_number_generation=true`` + ``POST /creditnotes/{id}/invoices`` (apply); debit note ->
  an invoice with ``reference_number="DN against {invoice}"`` (Zoho has no sales debit note, Known gap 3); payment ->
  ``POST /customerpayments`` with ``invoices[{invoice_id, amount_applied, tax_amount_withheld}]``.
* ``document_status`` -> ``GET /invoices/{id}``; ``pull_payments`` -> ``GET /customerpayments`` (paged, 200 per page)
  then ``GET /customerpayments/{id}``; ``masters_sync`` -> ``POST /contacts`` / ``PUT /contacts/{id}`` and, for an
  empty request, the ledger pickers (``/chartofaccounts``, ``/settings/taxes``, ``/items``).
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import (
    ConnectorAuthError,
    ConnectorPermanentError,
    ConnectorRateLimited,
    ConnectorTransientError,
)
from app.core.net_safety import SafeResponse
from app.services.connectors.accounting.port import LedgerDocumentDTO, LedgerPaymentDTO, LedgerResultDTO

__all__ = ["API_BASE", "EGRESS_HOSTS", "SCOPES", "TOKEN_URL", "ZohoBooks", "ZohoBooksConfig"]

API_BASE = "https://www.zohoapis.in/books/v3"
TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
AUTHORIZE_URL = "https://accounts.zoho.in/oauth/v2/auth"
EGRESS_HOSTS = ("www.zohoapis.in", "accounts.zoho.in")
SCOPES = (
    "ZohoBooks.contacts.ALL", "ZohoBooks.invoices.ALL", "ZohoBooks.creditnotes.ALL", "ZohoBooks.customerpayments.READ",
    "ZohoBooks.customerpayments.CREATE", "ZohoBooks.settings.READ",
)  # fmt: skip
PAGE_SIZE = 200
_ZOHO_MODES = {"neft": "banktransfer", "rtgs": "banktransfer", "imps": "banktransfer", "upi": "banktransfer", "cheque": "check", "wire": "banktransfer"}
_FROM_ZOHO_MODE = {"banktransfer": "neft", "bank transfer": "neft", "check": "cheque", "cheque": "cheque", "upi": "upi", "imps": "imps", "rtgs": "rtgs", "neft": "neft", "wire": "wire"}


class ZohoBooksConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str = Field(default="", max_length=40, pattern=r"^[0-9]*$")
    push_mode: Literal["auto", "manual"] = "manual"


def _check(resp: SafeResponse, what: str) -> Mapping[str, Any]:
    code = resp.status_code
    if code == 401:
        msg = f"Zoho Books rejected the credentials ({what})"
        raise ConnectorAuthError(msg)
    if code == 429:
        retry = resp.headers.get("retry-after", "60")
        raise ConnectorRateLimited(float(retry) if retry.strip().isdigit() else 60.0)
    if code >= 500:
        msg = f"Zoho Books error {code} ({what})"
        raise ConnectorTransientError(msg)
    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    if not isinstance(body, Mapping):
        body = {}
    zcode = body.get("code", 0 if 200 <= code < 300 else code)
    if code == 403 or zcode in (14, 57):  # 57: not authorised for the organisation
        msg = f"Zoho Books refused access ({what})"
        raise ConnectorAuthError(msg)
    if zcode not in (0, "0"):
        message = str(body.get("message") or f"Zoho Books refused the request ({what})")[:300]
        raise ConnectorPermanentError(f"zoho_{zcode}", message)
    return body  # type: ignore[no-any-return]


def _dec(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    return Decimal(str(v))


class ZohoBooks:
    category = "accounting"
    provider_key = "zoho_books"

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.key = self.provider_key
        self._token: str | None = None
        self._token_expires: datetime | None = None

    def __repr__(self) -> str:
        return f"ZohoBooks(mode={self.ctx.mode})"

    @property
    def _org(self) -> str:
        org = getattr(self.ctx.config, "organization_id", "")
        if not org:
            msg = "The Zoho Books organisation is not configured"
            raise ConnectorAuthError(msg)
        return str(org)

    async def _auth(self) -> dict[str, str]:
        now = self.ctx.now()
        if self._token and self._token_expires and now < self._token_expires:
            return {"Authorization": f"Zoho-oauthtoken {self._token}"}
        s = self.ctx.secrets
        refresh, client_id, client_secret = s.get("oauth_refresh"), s.get("client_id"), s.get("client_secret")
        if not (refresh and client_id and client_secret):
            msg = "Connect Zoho Books again: the authorisation is missing"
            raise ConnectorAuthError(msg)
        resp = await self.ctx.http.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id, "client_secret": client_secret},
        )
        if resp.status_code >= 400:
            _check(resp, "token")
        body = resp.json() if resp.content else {}
        token = body.get("access_token") if isinstance(body, Mapping) else None
        if not token:
            msg = "Connect Zoho Books again: the token was refused"
            raise ConnectorAuthError(msg)
        self._token = str(token)
        self._token_expires = now + timedelta(seconds=max(int(body.get("expires_in") or 3600) - 60, 60))
        return {"Authorization": f"Zoho-oauthtoken {self._token}"}

    async def _call(self, method: str, path: str, what: str, *, params: Mapping[str, Any] | None = None, json: Any = None) -> Mapping[str, Any]:
        headers = await self._auth()
        query = {"organization_id": self._org, **dict(params or {})}
        kw: dict[str, Any] = {"headers": headers, "params": query}
        if json is not None:
            kw["json"] = json
        resp = await self.ctx.http.request(method, f"{API_BASE}{path}", **kw)
        return _check(resp, what)

    # ----------------------------------------------------------------------------------------- ConnectorPort
    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        if op == "health":
            await self._call("GET", "/organizations", "organisations")
            return {"ok": True}
        raise NotImplementedError(op)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def export(self, rows: Sequence[Mapping[str, Any]], template: Mapping[str, Any]) -> bytes:
        """The P2 accounting export stays a file (CSV / XLSX via the Manual path); Zoho has no file import here."""
        raise NotImplementedError("zoho_books: use the accounting export download")

    # ------------------------------------------------------------------------------------ AccountingSyncPort
    @staticmethod
    def _lines(p: Mapping[str, Any]) -> list[dict[str, Any]]:
        out = []
        for line in p.get("lines") or []:
            item: dict[str, Any] = {
                "description": str(line.get("description") or "")[:2000],
                "hsn_or_sac": line.get("sac"),
                "quantity": float(Decimal(str(line.get("quantity") or 1))),
                "rate": float(Decimal(str(line.get("rate") or 0))),
            }
            if line.get("item_ref"):
                item["item_id"] = line["item_ref"]
            else:
                item["name"] = str(line.get("description") or "Services")[:100]
            if line.get("revenue_ref"):
                item["account_id"] = line["revenue_ref"]
            if line.get("tax_ref"):
                item["tax_id"] = line["tax_ref"]
            if line.get("discount"):
                item["discount"] = float(Decimal(str(line["discount"])))
            out.append(item)
        return out

    def _invoice_body(self, d: LedgerDocumentDTO) -> dict[str, Any]:
        p = d.payload
        notes = []
        if p.get("irn"):
            notes.append(f"IRN: {p['irn']}")
        if p.get("ack_no"):
            notes.append(f"Ack no.: {p['ack_no']}")
        body: dict[str, Any] = {
            "customer_id": d.customer_ref,
            "invoice_number": d.number,
            "date": d.issue_date.isoformat(),
            "is_inclusive_tax": False,
            "line_items": self._lines(p),
        }
        for src, dst in (("due_date", "due_date"), ("place_of_supply", "place_of_supply"), ("gst_treatment", "gst_treatment"), ("gst_no", "gst_no"), ("reverse_charge_tax_ref", "reverse_charge_tax_id"), ("exchange_rate", "exchange_rate")):
            if p.get(src) not in (None, ""):
                body[dst] = p[src]
        if p.get("round_off") not in (None, "", "0", "0.00"):
            body["adjustment"] = float(Decimal(str(p["round_off"])))
            body["adjustment_description"] = "Round off"
        reference = p.get("reference_number")
        if d.kind == "debit_note":
            reference = f"DN against {p.get('linked_invoice_number') or ''}".strip()
        if reference:
            body["reference_number"] = str(reference)[:100]
        if notes:
            body["notes"] = "; ".join(notes)
        return body

    async def _push_invoice(self, d: LedgerDocumentDTO) -> LedgerResultDTO:
        p = d.payload
        action, existing = p.get("action") or "create", p.get("external_ref")
        if action == "void" and existing:
            await self._call("POST", f"/invoices/{existing}/status/void", "void invoice")
            return LedgerResultDTO(d.document_id, str(existing), "synced", "Voided in Zoho Books")
        if action == "update" and existing:
            await self._call("PUT", f"/invoices/{existing}", "update invoice", json=self._invoice_body(d))
            return LedgerResultDTO(d.document_id, str(existing), "synced", None)
        body = await self._call("POST", "/invoices", "create invoice", params={"ignore_auto_number_generation": "true"}, json=self._invoice_body(d))
        inv = body.get("invoice") or {}
        ref = str(inv.get("invoice_id") or "")
        if not ref:
            raise ConnectorPermanentError("zoho_no_id", "Zoho Books did not return the invoice id")
        await self._call("POST", f"/invoices/{ref}/status/sent", "mark invoice sent")
        message = "Pushed as an invoice (Zoho Books has no sales debit note)" if d.kind == "debit_note" else None
        return LedgerResultDTO(d.document_id, ref, "synced", message)

    async def _push_credit_note(self, d: LedgerDocumentDTO) -> LedgerResultDTO:
        p = d.payload
        if p.get("action") == "void" and p.get("external_ref"):
            await self._call("POST", f"/creditnotes/{p['external_ref']}/status/void", "void credit note")
            return LedgerResultDTO(d.document_id, str(p["external_ref"]), "synced", "Voided in Zoho Books")
        body = {
            "customer_id": d.customer_ref,
            "creditnote_number": d.number,
            "date": d.issue_date.isoformat(),
            "line_items": self._lines(p),
            "reason": str(p.get("reason") or "")[:200] or None,
        }
        for src, dst in (("place_of_supply", "place_of_supply"), ("gst_treatment", "gst_treatment"), ("gst_no", "gst_no"), ("linked_invoice_ref", "reference_invoice_id")):
            if p.get(src):
                body[dst] = p[src]
        created = await self._call("POST", "/creditnotes", "create credit note", params={"ignore_auto_number_generation": "true"}, json=body)
        ref = str((created.get("creditnote") or {}).get("creditnote_id") or "")
        if not ref:
            raise ConnectorPermanentError("zoho_no_id", "Zoho Books did not return the credit note id")
        if p.get("linked_invoice_ref"):
            total = p.get("total")
            await self._call(
                "POST", f"/creditnotes/{ref}/invoices", "apply credit note",
                json={"invoices": [{"invoice_id": p["linked_invoice_ref"], "amount_applied": float(Decimal(str(total or 0)))}]},
            )  # fmt: skip
        return LedgerResultDTO(d.document_id, ref, "synced", None)

    async def _push_payment(self, d: LedgerDocumentDTO) -> LedgerResultDTO:
        pay = d.payload.get("payment") or {}
        body: dict[str, Any] = {
            "customer_id": d.customer_ref,
            "payment_mode": _ZOHO_MODES.get(str(pay.get("mode") or ""), "banktransfer"),
            "amount": float(Decimal(str(pay.get("amount") or 0))),
            "date": d.issue_date.isoformat(),
            "reference_number": str(pay.get("reference") or d.number)[:100],
            "invoices": [
                {"invoice_id": a["invoice_ref"], "amount_applied": float(Decimal(str(a.get("amount") or 0))), "tax_amount_withheld": float(Decimal(str(a.get("tds") or 0)))}
                for a in pay.get("allocations") or []
                if a.get("invoice_ref")
            ],
        }
        if pay.get("bank_ref"):
            body["account_id"] = pay["bank_ref"]
        created = await self._call("POST", "/customerpayments", "record payment", json=body)
        ref = str((created.get("payment") or {}).get("payment_id") or "")
        return LedgerResultDTO(d.document_id, ref or None, "synced" if ref else "failed", None if ref else "Zoho Books did not return the payment id")

    async def push_documents(self, docs: Sequence[LedgerDocumentDTO]) -> Sequence[LedgerResultDTO]:
        out: list[LedgerResultDTO] = []
        for d in docs:
            try:
                if d.kind in ("invoice", "debit_note"):
                    out.append(await self._push_invoice(d))
                elif d.kind == "credit_note":
                    out.append(await self._push_credit_note(d))
                else:
                    out.append(await self._push_payment(d))
            except ConnectorPermanentError as exc:
                out.append(LedgerResultDTO(d.document_id, None, "failed", str(exc)[:500]))
        return out

    async def document_status(self, refs: Sequence[str]) -> Sequence[LedgerResultDTO]:
        out: list[LedgerResultDTO] = []
        for ref in refs:
            try:
                body = await self._call("GET", f"/invoices/{ref}", "invoice status")
            except ConnectorPermanentError as exc:
                out.append(LedgerResultDTO(uuid.UUID(int=0), ref, "failed", str(exc)[:300]))
                continue
            inv = body.get("invoice") or {}
            message = f"{inv.get('status') or ''}|{inv.get('total') or ''}|{inv.get('balance') or ''}"
            out.append(LedgerResultDTO(uuid.UUID(int=0), ref, "synced", message))
        return out

    async def pull_payments(self, since: datetime | None, cursor: str | None) -> tuple[Sequence[LedgerPaymentDTO], str | None]:
        page = int(cursor) if cursor and cursor.isdigit() else 1
        params: dict[str, Any] = {"page": page, "per_page": PAGE_SIZE, "sort_column": "last_modified_time"}
        if since is not None:
            params["last_modified_time"] = since.strftime("%Y-%m-%dT%H:%M:%S%z")
        body = await self._call("GET", "/customerpayments", "list payments", params=params)
        out: list[LedgerPaymentDTO] = []
        for row in body.get("customerpayments") or []:
            pid = row.get("payment_id")
            if not pid:
                continue
            detail = (await self._call("GET", f"/customerpayments/{pid}", "payment detail")).get("payment") or {}
            invoices = detail.get("invoices") or [{}]
            for inv in invoices:
                applied = _dec(inv.get("amount_applied")) or _dec(detail.get("amount")) or Decimal(0)
                if applied <= 0:
                    continue
                ext = str(pid) if len(invoices) == 1 else f"{pid}:{inv.get('invoice_id')}"
                out.append(
                    LedgerPaymentDTO(
                        external_ref=ext,
                        invoice_number=str(inv.get("invoice_number") or detail.get("invoice_numbers") or ""),
                        invoice_external_ref=str(inv["invoice_id"]) if inv.get("invoice_id") else None,
                        amount=applied,
                        currency=str(detail.get("currency_code") or "INR"),
                        paid_on=date.fromisoformat(str(detail.get("date") or row.get("date"))[:10]),
                        tds_amount=_dec(inv.get("tax_amount_withheld")),
                        mode=_FROM_ZOHO_MODE.get(str(detail.get("payment_mode") or "").lower()),
                        reference=str(detail.get("reference_number") or "")[:120] or None,
                    )
                )
        more = bool((body.get("page_context") or {}).get("has_more_page"))
        return out, (str(page + 1) if more else None)

    async def masters_sync(self, customers: Sequence[Mapping[str, Any]], ledgers: Sequence[Mapping[str, Any]]) -> Mapping[str, str]:
        if not customers and not ledgers:
            out: dict[str, str] = {}
            for path, key, id_key, name_key, prefix in (
                ("/chartofaccounts", "chartofaccounts", "account_id", "account_name", "account"),
                ("/settings/taxes", "taxes", "tax_id", "tax_name", "tax"),
                ("/items", "items", "item_id", "name", "item"),
            ):
                body = await self._call("GET", path, f"{prefix} list")
                for row in body.get(key) or []:
                    if row.get(id_key):
                        out[f"{prefix}:{row[id_key]}"] = str(row.get(name_key) or row[id_key])[:200]
            return out
        result: dict[str, str] = {}
        for c in customers:
            gstin = c.get("gstin")
            treatment = "overseas" if c.get("foreign") else ("business_gst" if gstin else "business_none")
            body = {
                "contact_name": c.get("name"),
                "company_name": c.get("name"),
                "contact_type": "customer",
                "gst_treatment": treatment,
                "gst_no": gstin or None,
                "place_of_contact": c.get("state_code"),
                "billing_address": c.get("address") or {},
            }
            if c.get("external_ref"):
                await self._call("PUT", f"/contacts/{c['external_ref']}", "update contact", json=body)
                result[str(c["key"])] = str(c["external_ref"])
            else:
                created = await self._call("POST", "/contacts", "create contact", json=body)
                cid = (created.get("contact") or {}).get("contact_id")
                if cid:
                    result[str(c["key"])] = str(cid)
        return result

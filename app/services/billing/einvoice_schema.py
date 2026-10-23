# ruff: file-ignore[line-too-long]
"""Pure mapping of an issued document to the government e-invoice JSON (INV-01 v1.1, FR-151). INR values come from
the stored INR columns (never recomputed). No DB, no I/O."""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

DOC_TYPE = {"invoice": "INV", "credit_note": "CRN", "debit_note": "DBN"}
SUP_TYPE = {"b2b": "B2B", "sezwp": "SEZWP", "sezwop": "SEZWOP", "expwp": "EXPWP", "expwop": "EXPWOP"}
REQUIRED_TOP = ("Version", "TranDtls", "DocDtls", "SellerDtls", "BuyerDtls", "ItemList", "ValDtls")


def _d(value: Any) -> Decimal:
    """Amounts at 2 dp; serialized as exact JSON numbers by ``dumps`` (never via float)."""
    return Decimal(value or 0).quantize(Decimal("0.01"))


def dumps(payload: Any) -> str:
    """JSON with Decimals written as exact numbers (the schema takes numbers; floats are never used)."""
    text = json.dumps(
        payload, default=lambda v: f"__DEC__{v}__" if isinstance(v, Decimal) else str(v), ensure_ascii=False
    )
    return re.sub(r'"__DEC__(-?[0-9.E+-]+)__"', lambda m: m.group(1), text)


def _dt(d: date | None) -> str | None:
    return d.strftime("%d/%m/%Y") if d else None


def _party(snap: Mapping[str, Any], gstin_default: str = "URP") -> dict[str, Any]:
    address = snap.get("address") or snap.get("registration_address") or {}
    if not isinstance(address, Mapping):
        address = {}
    return {
        "Gstin": snap.get("gstin") or gstin_default,
        "LglNm": snap.get("legal_name") or snap.get("name") or "",
        "Addr1": str(address.get("line1") or address.get("address_line1") or snap.get("name") or "")[:100]
        or "NA",
        "Loc": str(address.get("city") or "NA")[:50],
        "Pin": int(str(address.get("postal_code") or address.get("pin") or "999999")[:6] or 999999)
        if str(address.get("postal_code") or address.get("pin") or "999999")[:6].isdigit()
        else 999999,
        "Stcd": snap.get("state_code") or "96",
    }


def build(
    kind: str, doc: Any, lines: Sequence[Any], taxes: Sequence[Any], *, original: Any | None = None
) -> dict[str, Any]:
    by_line: dict[Any, dict[str, Any]] = {}
    for t in taxes:
        by_line.setdefault(t.line_id, {})[t.component] = t
    inr = doc.currency == "INR"
    items = []
    oth = Decimal(0)
    for ln in lines:
        comps = by_line.get(ln.id, {})
        if ln.outside_taxable_value:
            oth += Decimal(ln.inr_taxable_value)
            continue
        ass = Decimal(ln.inr_taxable_value)
        igst = Decimal(comps["igst"].inr_amount) if "igst" in comps else Decimal(0)
        cgst = Decimal(comps["cgst"].inr_amount) if "cgst" in comps else Decimal(0)
        sgst = sum((Decimal(comps[c].inr_amount) for c in ("sgst", "utgst") if c in comps), Decimal(0))
        gross = Decimal(ln.gross_amount) if inr else ass
        discount = Decimal(ln.discount_amount) if inr else Decimal(0)
        items.append(
            {
                "SlNo": str(ln.line_no),
                "PrdDesc": ln.description[:300],
                "IsServc": "Y",
                "HsnCd": ln.sac_code,
                "Qty": _d(ln.quantity),
                "Unit": ln.uqc or "OTH",
                "UnitPrice": Decimal(ln.effective_rate)
                if inr
                else _d(ass / Decimal(ln.quantity) if Decimal(ln.quantity) else ass),
                "TotAmt": _d(gross),
                "Discount": _d(discount),
                "AssAmt": _d(ass),
                "GstRt": Decimal(ln.tax_rate_pct).normalize() if Decimal(ln.tax_rate_pct) else Decimal(0),
                "IgstAmt": _d(igst),
                "CgstAmt": _d(cgst),
                "SgstAmt": _d(sgst),
                "TotItemVal": _d(ass + igst + cgst + sgst),
            }
        )
    val = {
        "AssVal": _d(doc.inr_taxable_total),
        "CgstVal": _d(sum((Decimal(t.inr_amount) for t in taxes if t.component == "cgst"), Decimal(0))),
        "SgstVal": _d(
            sum((Decimal(t.inr_amount) for t in taxes if t.component in ("sgst", "utgst")), Decimal(0))
        ),
        "IgstVal": _d(sum((Decimal(t.inr_amount) for t in taxes if t.component == "igst"), Decimal(0))),
        "OthChrg": _d(oth),
        "RndOffAmt": _d(doc.round_off if inr else 0),
        "TotInvVal": _d(doc.inr_document_total),
    }
    if not inr:
        val["TotInvValFc"] = _d(doc.document_total)
    recipient = dict(doc.recipient_snapshot or {})
    buyer = _party(recipient)
    buyer["Pos"] = doc.pos_state_code or "96"
    payload: dict[str, Any] = {
        "Version": "1.1",
        "TranDtls": {
            "TaxSch": "GST",
            "SupTyp": SUP_TYPE.get(doc.supply_type or "", "B2B"),
            "RegRev": "Y" if doc.reverse_charge else "N",
            "IgstOnIntra": "N",
        },
        "DocDtls": {"Typ": DOC_TYPE[kind], "No": doc.number, "Dt": _dt(doc.doc_date)},
        "SellerDtls": _party(dict(doc.supplier_snapshot or {})),
        "BuyerDtls": buyer,
        "ItemList": items,
        "ValDtls": val,
    }
    if (doc.supply_type or "").startswith("exp"):
        payload["ExpDtls"] = {"ForCur": doc.currency, "CntCode": (recipient.get("country") or "")[:2] or None}
    if original is not None:
        payload["RefDtls"] = {"PrecDocDtls": [{"InvNo": original.number, "InvDt": _dt(original.doc_date)}]}
    return payload


def validate(payload: Mapping[str, Any]) -> list[str]:
    """Structural checks of the subset we generate (required blocks, document fields, item totals)."""
    errors = [f"missing:{k}" for k in REQUIRED_TOP if k not in payload]
    doc = payload.get("DocDtls") or {}
    if doc.get("Typ") not in ("INV", "CRN", "DBN") or not doc.get("No") or not doc.get("Dt"):
        errors.append("DocDtls")
    if len(str(doc.get("No") or "")) > 16:
        errors.append("DocDtls.No")
    if not payload.get("ItemList") and not (payload.get("ValDtls") or {}).get("OthChrg"):
        errors.append("ItemList")
    for item in payload.get("ItemList") or []:
        for key in ("SlNo", "IsServc", "HsnCd", "AssAmt", "GstRt", "TotItemVal"):
            if key not in item:
                errors.append(f"ItemList.{key}")
    if len(str((payload.get("SellerDtls") or {}).get("Gstin") or "")) != 15:
        errors.append("SellerDtls.Gstin")
    return errors

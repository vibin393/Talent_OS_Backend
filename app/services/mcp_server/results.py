# ruff: noqa: E501, D401
"""``ExternalCallOut`` -> MCP ``CallToolResult`` (Epic 73, spec 0.4). Pure.

* A defence-in-depth redaction pass after the runtime's masking: Aadhaar, PAN, passport, IFSC and bank account
  numbers are always replaced by ``[redacted]``; contact fields unless the token holds ``pii.contact:read``.
* ``max_results`` slicing of ``rows`` / ``hits`` / ``data`` / ``items`` arrays (``talentos/truncated``).
* ``talentos/untrusted`` when the result carries runtime untrusted-content delimiters.
* No instruction text is ever added to a tool output; every message here is static.
"""

import json
import re
from collections.abc import Collection
from typing import Any

from app.services.mcp_server.catalog import SCOPE_LABELS

REDACTED = "[redacted]"
UNTRUSTED_MARK = "<untrusted_document"
LIST_KEYS = ("rows", "hits", "data", "items", "results", "candidates", "matches")
CONTACT_SCOPE = "pii.contact:read"

_BOUND_L = r"(?<![A-Za-z0-9_\-])"
_BOUND_R = r"(?![A-Za-z0-9_\-])"
_AADHAAR = re.compile(_BOUND_L + r"([2-9][0-9]{3})([ ]?)([0-9]{4})\2([0-9]{4})" + _BOUND_R)
_PAN = re.compile(_BOUND_L + r"[A-Z]{5}[0-9]{4}[A-Z]" + _BOUND_R)
_PASSPORT = re.compile(_BOUND_L + r"[A-Z][0-9]{7}" + _BOUND_R)
_IFSC = re.compile(_BOUND_L + r"[A-Z]{4}0[A-Z0-9]{6}" + _BOUND_R)
_BANK_DIGITS = re.compile(r"^[0-9 \-]{9,24}$")
_ACCOUNT_KEY = re.compile(r"(?i)(^|_)(account|acct|bank_account)(_|$)|account_?(no|number)")
_CONTACT_KEY = re.compile(r"(?i)^(emails?|phones?|mobiles?|phone_numbers?|.*_emails?|.*_phones?|.*_mobiles?)$")

ERROR_TEXT = {
    "policy_violation": "This tool isn't available in this session.",
    "not_allowlisted": "This tool isn't available in this session.",
    "forbidden": "You don't have access to do this in Talent OS.",
    "no_permission": "You don't have access to do this in Talent OS.",
    "owner_unavailable": "You don't have access to do this in Talent OS.",
    "consent_missing": "This candidate hasn't consented to AI processing, so this tool can't use their record.",
    "kill_switch": "AI tools are paused by your organisation.",
    "global_kill": "AI tools are paused by your organisation.",
    "feature_off": "AI tools are paused by your organisation.",
    "agent_off": "AI tools are paused by your organisation.",
    "session_closed": "This session has ended. Reconnect the assistant to continue.",
}


def _date_like(digits: str) -> bool:
    """A 12-digit run that reads as ``YYYYMMDDhhmm`` is a timestamp, not an Aadhaar number."""
    if len(digits) != 12 or digits[:2] not in ("19", "20"):
        return False
    month, day, hour, minute = int(digits[4:6]), int(digits[6:8]), int(digits[8:10]), int(digits[10:12])
    return 1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59


class Redactor:
    def __init__(self, scopes: Collection[str]) -> None:
        self.contacts_ok = CONTACT_SCOPE in scopes
        self.count = 0

    def _text(self, value: str) -> str:
        def aadhaar(m: re.Match[str]) -> str:
            digits = m.group(1) + m.group(3) + m.group(4)
            if not m.group(2) and _date_like(digits):
                return m.group(0)
            self.count += 1
            return REDACTED

        def sub(m: re.Match[str]) -> str:  # noqa: ARG001
            self.count += 1
            return REDACTED

        out = _AADHAAR.sub(aadhaar, value)
        for pattern in (_PAN, _IFSC, _PASSPORT):
            out = pattern.sub(sub, out)
        return out

    def _field(self, key: str, value: Any) -> Any:
        if _CONTACT_KEY.match(key) and not self.contacts_ok and value not in (None, "", [], {}):
            self.count += 1
            return REDACTED
        if _ACCOUNT_KEY.search(key) and isinstance(value, str | int) and _BANK_DIGITS.match(str(value)):
            digits = re.sub(r"[^0-9]", "", str(value))
            if 9 <= len(digits) <= 18:
                self.count += 1
                return REDACTED
        return self.walk(value)

    def walk(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {k: self._field(str(k), v) for k, v in node.items()}
        if isinstance(node, list):
            return [self.walk(v) for v in node]
        if isinstance(node, str):
            return self._text(node)
        return node


def redact(data: Any, scopes: Collection[str]) -> tuple[Any, int]:
    r = Redactor(scopes)
    return r.walk(data), r.count


def cap_lists(data: Any, max_results: int) -> tuple[Any, bool]:
    """Slice the known list keys to ``max_results`` at any depth."""
    truncated = False

    def walk(node: Any) -> Any:
        nonlocal truncated
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in LIST_KEYS and isinstance(v, list) and len(v) > max_results:
                    truncated = True
                    v = v[:max_results]
                out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(data), truncated


def has_untrusted(data: Any) -> bool:
    return UNTRUSTED_MARK in json.dumps(data, ensure_ascii=False, default=str)


def canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":"))


def text_result(text: str, *, is_error: bool, meta: dict[str, Any] | None = None, structured: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        out["structuredContent"] = structured
    if meta:
        out["_meta"] = meta
    return out


def refusal_text(error_code: str | None, *, mcp_scope: str | None = None) -> str:
    if error_code == "insufficient_scope":
        label = SCOPE_LABELS.get(mcp_scope or "", mcp_scope or "this permission")
        return f"This connection wasn't given permission for this tool. Reconnect the assistant and allow '{label}'."
    return ERROR_TEXT.get(error_code or "", "You don't have access to do this in Talent OS.")


def _invalid_fields(result: dict[str, Any] | None) -> str:
    fields: list[str] = []
    raw = result.get("fields") if isinstance(result, dict) else None
    if isinstance(raw, list):
        fields = [str(f) for f in raw][:20]
    return ", ".join(fields) or "unknown"


def map_outcome(
    out: Any, *, scopes: Collection[str], max_results: int, mcp_scope: str | None, proposal_text: str | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:  # fmt: skip
    """Return the ``CallToolResult`` and the counter deltas (``refusal_count``, ``redaction_count`` ...)."""
    outcome = out.outcome
    deltas: dict[str, int] = {}
    if outcome == "executed":
        data, n = redact(out.result or {}, scopes)
        data, sliced = cap_lists(data, max_results)
        meta: dict[str, Any] = {
            "talentos/untrusted": has_untrusted(data),
            "talentos/truncated": bool(out.truncated or sliced),
        }
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if cursor:
            meta["talentos/next_cursor"] = cursor
        if n:
            deltas["redaction_count"] = n
        return text_result(canonical(data), is_error=False, meta=meta, structured=data), deltas
    if outcome == "proposed":
        deltas["proposal_count"] = 1
        structured = {"status": "proposed", "proposal_id": str(out.proposal_id), "proposal_url": out.proposal_url}
        text = proposal_text or f"Created a proposal to review in Talent OS: {out.proposal_url}. Nothing has been done yet."
        return text_result(text, is_error=False, structured=structured), deltas
    if outcome == "deduplicated":
        data, n = redact(out.result or {"status": "already_handled"}, scopes)
        structured: dict[str, Any] = dict(data) if isinstance(data, dict) else {"status": "already_handled"}
        if out.proposal_id is not None:
            structured.update({"status": "proposed", "proposal_id": str(out.proposal_id), "proposal_url": out.proposal_url})
            text = f"This request was already handled: proposal {out.proposal_url}. Nothing has been done yet."
        else:
            text = "This request was already handled in this session. " + canonical(structured)
        return text_result(text, is_error=False, structured=structured), deltas
    deltas["refusal_count"] = 1
    if outcome == "refused":
        if out.error_code in ("policy_violation", "not_allowlisted"):
            deltas["violation_count"] = 1
        return text_result(refusal_text(out.error_code, mcp_scope=mcp_scope), is_error=True), deltas
    if outcome == "invalid":
        return text_result(f"The arguments don't match the tool's schema: {_invalid_fields(out.result)}", is_error=True), deltas
    if outcome == "not_found":
        return text_result("No matching record was found.", is_error=True), deltas
    return text_result("The tool failed. Try again later.", is_error=True), deltas


__all__ = [
    "REDACTED",
    "Redactor",
    "canonical",
    "cap_lists",
    "has_untrusted",
    "map_outcome",
    "redact",
    "refusal_text",
    "text_result",
]

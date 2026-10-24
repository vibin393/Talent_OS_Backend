# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared base of the real job board adapters (Epic 70, spec A2): Naukri, LinkedIn and Indeed.

Adapters see only ``core.integrations``, ``core.net_safety``, ``core.signing``, the job board DTOs and their own
package (``p5-connectors-isolation``). Every HTTP call goes through ``AdapterContext.http`` (the Egress Guard limited
to the manifest's ``egress_hosts``). Credentials and applicant data never appear in ``repr``, logs or exception
messages; board error messages are sanitised (``sanitise_message``) before they reach a Board Posting.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import (
    ConnectorAuthError,
    ConnectorPermanentError,
    ConnectorRateLimited,
    ConnectorTransientError,
)
from app.core.net_safety import SafeHttpClient, SafeResponse
from app.services.connectors.job_board.port import BoardApplicantDTO, JobPostingDTO

__all__ = [
    "LiveJobBoardBase",
    "classify",
    "description_subset",
    "lakhs_per_annum",
    "parse_dt",
    "sanitise_message",
]

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_TAG = re.compile(r"<(/?)([a-zA-Z0-9]+)[^>]*>")  # NOSONAR


def sanitise_message(text: Any, limit: int = 500) -> str:
    """A board's message as plain, single-line text (no markup, no control characters, capped)."""
    value = _CTRL.sub(" ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()
    return re.sub(r"\s+", " ", value)[:limit]


def description_subset(html: str, allowed: frozenset[str], limit: int | None = None) -> str:
    """Keep only ``allowed`` tags (attributes dropped) of already sanitised HTML."""

    def _keep(m: re.Match[str]) -> str:
        tag = m.group(2).lower()
        if tag not in allowed:
            return ""
        return f"<{m.group(1)}{tag}>" if tag != "br" else "<br>"

    out = _TAG.sub(_keep, html or "")
    return out[:limit] if limit else out


def lakhs_per_annum(amount: Decimal | None, unit: str) -> Decimal | None:
    """Naukri CTC (lakhs per annum): year -> /100,000; month -> x12/100,000; day / hour -> not sent."""
    if amount is None:
        return None
    if unit == "year":
        return (amount / Decimal(100000)).quantize(Decimal("0.01"))
    if unit == "month":
        return (amount * 12 / Decimal(100000)).quantize(Decimal("0.01"))
    return None


def parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / (1000 if value > 10**11 else 1), tz=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def classify(resp: SafeResponse, what: str) -> None:
    """Raise the classified connector error for a non-2xx board response."""
    code = resp.status_code
    if 200 <= code < 300:
        return
    if code in (401, 403):
        msg = f"The board rejected the credentials ({what})"
        raise ConnectorAuthError(msg)
    if code == 429:
        retry = resp.headers.get("retry-after", "60")
        raise ConnectorRateLimited(float(retry) if retry.strip().isdigit() else 60.0)
    if code >= 500:
        msg = f"board error {code} ({what})"
        raise ConnectorTransientError(msg)
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, Mapping):
            detail = str(body.get("message") or body.get("error_description") or body.get("error") or "")
    except ValueError:
        detail = ""
    raise ConnectorPermanentError("board_validation", sanitise_message(detail) or f"The board refused the request ({what})")


class LiveJobBoardBase:
    """Common plumbing: ``ConnectorPort.handle`` (only ``health``), no webhooks unless overridden, safe ``repr``."""

    category: ClassVar[str] = "job_board"
    provider_key: ClassVar[str] = ""

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.key = type(self).provider_key

    def __repr__(self) -> str:
        return f"{type(self).__name__}(mode={self.ctx.mode})"

    @property
    def http(self) -> SafeHttpClient:
        return self.ctx.http

    def secret(self, name: str) -> str:
        value = self.ctx.secrets.get(name)
        if not value:
            msg = f"The {name.replace('_', ' ')} is not configured"
            raise ConnectorAuthError(msg)
        return value

    def sibling_client(self, max_bytes: int) -> SafeHttpClient:
        """Same egress hosts and transport, another size cap (resume downloads)."""
        http = self.ctx.http
        return SafeHttpClient(
            allowed_hosts=http.allowed_hosts, max_redirects=http.max_redirects, max_bytes=max_bytes,
            purpose=http.purpose, transport=http._transport,
        )  # fmt: skip

    async def health(self) -> None:  # pragma: no cover - overridden  # NOSONAR
        return None

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        if op == "health":
            await self.health()
            return {"ok": True}
        msg = f"job_board op {op} runs through the job board sync handlers"
        raise ConnectorPermanentError("op_unsupported", msg)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def fetch_applicants(  # NOSONAR
        self,
        since: datetime | None,  # ruff: ignore[unused-method-argument]
        cursor: str | None,  # ruff: ignore[unused-method-argument]
    ) -> tuple[Sequence[BoardApplicantDTO], str | None]:
        return (), None

    @staticmethod
    def location_text(posting: JobPostingDTO) -> str | None:
        loc = posting.location
        if loc is None:
            return None
        return ", ".join(p for p in (loc.city, loc.region, loc.country) if p) or None

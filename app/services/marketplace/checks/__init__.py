# ruff: file-ignore[line-too-long]
"""Automated checks for partner versions and private Apps (Epic 69, spec A7 / A4 step 4, FR-358).

``run_manifest_checks`` (pure, private Apps at install) covers ``manifest_schema``, ``scope_reasons``,
``sensitive_justification`` and ``redirect_uris``. ``run_version_checks`` adds ``urls_reachable`` (through the Egress
Guard, ``SafeHttpClient``), ``conformance_report``, ``listing_text`` (human-only automation lexicon), ``assets_scan`` and
``transfer_statement``. Results are ``[{key, passed, detail}]``.
"""

import dataclasses
import functools
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core.errors import DomainError
from app.core.net_safety import SafeHttpClient
from app.core.oauth_scopes import is_sensitive, scopes

SECURITY_QUESTIONS = (
    "data_encrypted_at_rest",
    "data_encrypted_in_transit",
    "access_reviews",
    "incident_response_contact",
    "subprocessors_listed",
    "data_deletion_on_request",
)
MAX_LOGO_BYTES = 1_048_576
MAX_SCREENSHOT_BYTES = 2_097_152
MAX_SCREENSHOTS = 5
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


@dataclasses.dataclass(frozen=True, slots=True)
class CheckResult:
    key: str
    passed: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "passed": self.passed, "detail": self.detail}


@functools.cache
def human_only_claims() -> tuple[str, ...]:
    text = (Path(__file__).parent / "human_only_claims.txt").read_text(encoding="utf-8")
    return tuple(
        line.strip().lower() for line in text.splitlines() if line.strip() and not line.startswith("#")
    )


def _flows(app: Any) -> list[dict[str, Any]]:
    return list((getattr(app, "manifest", None) or {}).get("data_flows") or [])


def check_manifest_schema(app: Any) -> CheckResult:
    manifest = getattr(app, "manifest", None)
    if not isinstance(manifest, dict):
        return CheckResult("manifest_schema", False, "The app has no manifest")
    catalogue = scopes()
    unknown = sorted(s for s in getattr(app, "requested_scopes", []) or [] if s not in catalogue)
    if unknown:
        return CheckResult("manifest_schema", False, "Unknown scopes: " + ", ".join(unknown))
    for f in _flows(app):
        if (
            not isinstance(f, dict)
            or f.get("direction") not in ("in", "out")
            or not f.get("entity")
            or not f.get("purpose")
        ):
            return CheckResult(
                "manifest_schema", False, "A data flow is missing its direction, entity or purpose"
            )
    return CheckResult("manifest_schema", True)


def check_scope_reasons(app: Any) -> CheckResult:
    reasons = dict(getattr(app, "scope_reasons", None) or {})
    bad = [
        s
        for s in getattr(app, "requested_scopes", []) or []
        if s != "offline_access" and not 20 <= len(str(reasons.get(s) or "").strip()) <= 500
    ]
    if bad:
        return CheckResult(
            "scope_reasons", False, "Give a reason of 20-500 characters for: " + ", ".join(sorted(bad))
        )
    return CheckResult("scope_reasons", True)


def check_sensitive_justification(app: Any) -> CheckResult:
    reasons = dict(getattr(app, "scope_reasons", None) or {})
    sensitive = [s for s in getattr(app, "requested_scopes", []) or [] if s in scopes() and is_sensitive(s)]
    short = [s for s in sensitive if len(str(reasons.get(s) or "").strip()) < 50]
    if short:
        return CheckResult(
            "sensitive_justification",
            False,
            "Justify each sensitive scope in 50+ characters: " + ", ".join(sorted(short)),
        )
    if sensitive and not any(f.get("personal_data", True) for f in _flows(app)):
        return CheckResult(
            "sensitive_justification", False, "Sensitive scopes need a matching personal-data flow"
        )
    return CheckResult("sensitive_justification", True)


def check_redirect_uris(app: Any) -> CheckResult:
    client_type = getattr(app, "client_type", "confidential")
    for uri in getattr(app, "redirect_uris", None) or []:
        try:
            parts = urlsplit(uri)
        except ValueError:
            return CheckResult("redirect_uris", False, f"Invalid redirect URI: {uri[:100]}")
        host = (parts.hostname or "").lower()
        if "*" in uri or "#" in uri or not host:
            return CheckResult("redirect_uris", False, f"Invalid redirect URI: {uri[:100]}")
        if parts.scheme == "https":
            continue
        if parts.scheme == "http" and host in LOOPBACK_HOSTS and client_type == "public":
            continue
        return CheckResult(
            "redirect_uris", False, f"Use https (loopback only for public clients): {uri[:100]}"
        )
    return CheckResult("redirect_uris", True)


def run_manifest_checks(app: Any, *, kind: str = "private") -> list[CheckResult]:  # ruff: ignore[unused-function-argument] - same set today
    return [
        check_manifest_schema(app),
        check_scope_reasons(app),
        check_sensitive_justification(app),
        check_redirect_uris(app),
    ]


def check_listing_text(name: str, tagline: str, description: str) -> CheckResult:
    if len(name) > 120 or len(tagline) > 200 or len(description) > 8000:
        return CheckResult(
            "listing_text", False, "Name ≤ 120, tagline ≤ 200 and description ≤ 8,000 characters"
        )
    blob = f"{name}\n{tagline}\n{description}".lower()
    hits = [p for p in human_only_claims() if p in blob]
    if hits:
        return CheckResult(
            "listing_text", False, "Claims automation of human-only decisions: " + ", ".join(hits[:5])
        )
    return CheckResult("listing_text", True)


def check_transfer_statement(flows: list[dict[str, Any]], statement: str | None) -> CheckResult:
    abroad = any(str(f.get("destination_country") or "IN").upper() != "IN" for f in flows)
    if abroad and not (statement or "").strip():
        return CheckResult(
            "transfer_statement", False, "Data leaves India: an India data-transfer statement is required"
        )
    return CheckResult("transfer_statement", True)


async def check_urls_reachable(urls: dict[str, str | None], *, transport: Any = None) -> CheckResult:
    missing = [k for k in ("privacy_url", "dpa_url") if not urls.get(k)]
    if missing:
        return CheckResult("urls_reachable", False, "Required: " + ", ".join(missing))
    for name, url in urls.items():
        if not url:
            continue
        if not url.lower().startswith("https://"):
            return CheckResult("urls_reachable", False, f"{name} must use https")
        client = SafeHttpClient(
            max_redirects=3, max_bytes=65_536, purpose="marketplace:url_check", transport=transport
        )
        try:
            resp = await client.get(url)
        except DomainError as exc:
            return CheckResult(
                "urls_reachable", False, f"{name} refused: {getattr(exc, 'code', 'egress_refused')}"
            )
        except Exception as exc:
            return CheckResult("urls_reachable", False, f"{name} unreachable: {type(exc).__name__}")
        finally:
            await client.aclose()
        if not 200 <= resp.status_code < 300:
            return CheckResult("urls_reachable", False, f"{name} answered {resp.status_code}")
    return CheckResult("urls_reachable", True)


_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"


async def check_assets(logo_key: str | None, screenshot_keys: list[str]) -> CheckResult:
    from app.core import scanning
    from app.core.storage import get_storage

    if len(screenshot_keys) > MAX_SCREENSHOTS:
        return CheckResult("assets_scan", False, f"At most {MAX_SCREENSHOTS} screenshots")
    storage = get_storage()
    items = ([(logo_key, MAX_LOGO_BYTES)] if logo_key else []) + [
        (k, MAX_SCREENSHOT_BYTES) for k in screenshot_keys
    ]
    for key, limit in items:
        try:
            data = await storage.get(key)
        except Exception:
            return CheckResult("assets_scan", False, "An image could not be read")
        if not (data.startswith(_PNG) or data.startswith(_JPEG)):
            return CheckResult("assets_scan", False, "Images must be PNG or JPEG")
        if len(data) > limit:
            return CheckResult(
                "assets_scan", False, "An image is too large (logo ≤ 1 MB, screenshots ≤ 2 MB)"
            )
        result = await scanning.scan(data, str(key))
        if result.status not in ("clean", "skipped_dev"):
            return CheckResult("assets_scan", False, "An image did not pass the malware scan")
    return CheckResult("assets_scan", True)


__all__ = [
    "SECURITY_QUESTIONS",
    "CheckResult",
    "check_assets",
    "check_listing_text",
    "check_manifest_schema",
    "check_redirect_uris",
    "check_scope_reasons",
    "check_sensitive_justification",
    "check_transfer_statement",
    "check_urls_reachable",
    "human_only_claims",
    "run_manifest_checks",
]

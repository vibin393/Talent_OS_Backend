# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Conformance checks (Epic 74, story 74.4, spec 0.6): pure evaluation of a run's observations into the seven
required checks of ``talentos.conformance/1`` (69 §A8). Observations are platform records only: the calls the fault
middleware saw (time, method, path, status, token grant, Idempotency-Key, the route's required scopes), the injected
faults and the runner's webhook probes."""

import functools
import json
import re
from pathlib import Path
from typing import Any

REQUIRED = (
    "oauth_client_credentials",
    "oauth_authorization_code_pkce",
    "scope_minimality",
    "webhook_signature_verified",
    "idempotent_retry",
    "handles_429",
    "uninstall_cleanup",
)
RETRY_GAP_SECONDS = 5.0
MAX_CALLS_AFTER_UNINSTALL = 10
_PIN = Path(__file__).resolve().parents[4] / "openapi-public-v1.json"


@functools.lru_cache(maxsize=1)
def route_table() -> tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...]:
    """(method, path regex, required scopes) of every pinned public operation (``x-required-scopes``)."""
    try:
        doc = json.loads(_PIN.read_text(encoding="utf-8"))
    except OSError:
        return ()
    out = []
    for path, ops in (doc.get("paths") or {}).items():
        pattern = re.compile("^" + re.sub(r"\{[^/}]+\}", "[^/]+", path) + "/?$")
        for method, op in ops.items():
            if isinstance(op, dict):
                out.append((method.upper(), pattern, tuple(op.get("x-required-scopes") or ())))
    return tuple(out)


def scopes_for(method: str, path: str) -> list[str]:
    for m, pattern, scopes in route_table():
        if m == method and pattern.match(path):
            return list(scopes)
    return []


def observable_scopes() -> set[str]:
    return {s for _, _, scopes in route_table() for s in scopes}


def _ok(status: int | None) -> bool:
    return status is not None and 200 <= int(status) < 300


def _check(key: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"key": key, "passed": bool(passed), "detail": detail}


def evaluate(obs: dict[str, Any], *, grant_types: list[str], granted_scopes: list[str]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = list(obs.get("calls") or [])
    uninstalled_at = obs.get("uninstalled_at")
    before = [c for c in calls if uninstalled_at is None or c["t"] < uninstalled_at]
    after = [c for c in calls if uninstalled_at is not None and c["t"] >= uninstalled_at]
    hook = dict(obs.get("webhook") or {})
    out: list[dict[str, Any]] = []

    for key, grant in (("oauth_client_credentials", "client_credentials"), ("oauth_authorization_code_pkce", "authorization_code")):
        if grant not in grant_types:
            out.append(_check(key, True, "n/a"))
            continue
        used = any(c.get("g") == grant and _ok(c.get("s")) for c in before)
        out.append(_check(key, used, "A token was issued and used" if used else f"No successful call with a {grant.replace('_', ' ')} token"))

    observable = observable_scopes()
    exercised = {s for c in before if _ok(c.get("s")) for s in c.get("sc") or []}
    unused = sorted(s for s in granted_scopes if s in observable and s not in exercised)
    out.append(_check("scope_minimality", not unused, "Every granted scope was used" if not unused else "Unused scopes: " + ", ".join(unused)))

    if not hook.get("subscription"):
        out.append(_check("webhook_signature_verified", True, "n/a"))
    else:
        valid, invalid = hook.get("valid_status"), hook.get("invalid_status")
        good = _ok(valid) and invalid is not None and not _ok(invalid)
        detail = "Signed events accepted and a bad signature refused" if good else (
            "The valid test event was not accepted" if not _ok(valid) else "An event with an invalid signature was accepted")
        out.append(_check("webhook_signature_verified", good, detail))

    writes = any(s.endswith(":write") for s in granted_scopes)
    inj503 = obs.get("inj503")
    if not writes and inj503 is None:
        out.append(_check("idempotent_retry", True, "n/a"))
    elif inj503 is None:
        out.append(_check("idempotent_retry", False, "No POST request was made during the run"))
    else:
        key_ = inj503.get("key")
        retried = bool(key_) and any(c["m"] == "POST" and c.get("k") == key_ and c["t"] > inj503["t"] and not c.get("i") for c in before)
        out.append(_check("idempotent_retry", retried, "The POST was retried with the same Idempotency-Key" if retried else "The failed POST was not retried with the same Idempotency-Key"))

    inj429 = obs.get("inj429")
    if inj429 is None:
        out.append(_check("handles_429", False, "No GET request arrived after the first 2 minutes"))
    else:
        nxt = next((c for c in before if c["t"] > inj429["t"] and not c.get("i")), None)
        waited = nxt is not None and nxt["t"] - inj429["t"] >= RETRY_GAP_SECONDS
        out.append(_check("handles_429", waited, "Retry-After was respected" if waited else ("No request after the rate limit" if nxt is None else "The next call came before Retry-After")))

    calls_after = len(after)
    hook_ok = (not hook.get("subscription")) or _ok(hook.get("uninstall_status"))
    clean = hook_ok and calls_after <= MAX_CALLS_AFTER_UNINSTALL and uninstalled_at is not None
    detail = f"{calls_after} calls after uninstall" + ("" if hook_ok else "; the uninstall event was not acknowledged")
    out.append(_check("uninstall_cleanup", clean, detail if uninstalled_at is not None else "The app was not uninstalled"))
    return out


__all__ = ["REQUIRED", "evaluate", "observable_scopes", "route_table", "scopes_for"]

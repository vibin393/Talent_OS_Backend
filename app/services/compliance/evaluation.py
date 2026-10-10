"""The pure requirement matcher (Epic 21 A4, FR-116). No I/O: table-driven and order-independent.

A requirement applies when its ``client_id`` is NULL (tenant level) or equals the subject's client, it is
active and effective on the evaluation date, and every **non-empty** ``match`` list contains the fact
(case-insensitive); ``role_keywords`` match as substrings of the role title. Items of every applicable
requirement are unioned by ``(kind, ref)``; the strictest ``mandatory`` wins and the requirement ids collect.
"""

import dataclasses
import uuid
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any


@dataclasses.dataclass(frozen=True)
class Facts:
    client_id: uuid.UUID | None = None
    industry: str | None = None
    country: str | None = None
    worker_type: str | None = None
    engagement_kind: str | None = None
    role_title: str | None = None
    on_date: date | None = None


@dataclasses.dataclass(frozen=True)
class Item:
    kind: str
    ref: str
    label: str
    mandatory: bool
    requirement_ids: tuple[uuid.UUID, ...]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _get(req: Any, key: str, default: Any = None) -> Any:
    if isinstance(req, Mapping):
        return req.get(key, default)
    return getattr(req, key, default)


def matches(req: Any, facts: Facts) -> bool:
    if not _get(req, "active", True) or _get(req, "archived_at") is not None:
        return False
    eff = _get(req, "effective_from")
    if eff is not None and facts.on_date is not None and eff > facts.on_date:
        return False
    client_id = _get(req, "client_id")
    if client_id is not None and str(client_id) != str(facts.client_id):
        return False
    m: Mapping[str, Any] = _get(req, "match") or {}
    for key, fact in (
        ("industries", facts.industry),
        ("countries", facts.country),
        ("worker_types", facts.worker_type),
        ("engagement_kinds", facts.engagement_kind),
    ):
        wanted = {_norm(v) for v in (m.get(key) or []) if _norm(v)}
        if wanted and _norm(fact) not in wanted:
            return False
    keywords = [_norm(k) for k in (m.get("role_keywords") or []) if _norm(k)]
    title = _norm(facts.role_title)
    return not keywords or any(k in title for k in keywords)


def applicable(requirements: Iterable[Any], facts: Facts) -> list[Item]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for req in requirements:
        if not matches(req, facts):
            continue
        rid = _get(req, "id")
        for raw in _get(req, "items") or []:
            kind, ref = str(raw.get("kind")), str(raw.get("ref"))
            entry = merged.setdefault(
                (kind, ref), {"label": raw.get("label") or ref, "mandatory": False, "ids": set()}
            )
            entry["mandatory"] = entry["mandatory"] or bool(raw.get("mandatory", True))
            # deterministic label: the alphabetically first one (order independence)
            entry["label"] = min(entry["label"], str(raw.get("label") or ref))
            if rid is not None:
                entry["ids"].add(uuid.UUID(str(rid)))
    return [
        Item(kind, ref, e["label"], e["mandatory"], tuple(sorted(e["ids"], key=str)))
        for (kind, ref), e in sorted(merged.items())
    ]


def summary(items: Iterable[Item]) -> dict[str, tuple[str, ...]]:
    """Refs per kind (for ``ComplianceRequirementsEvaluated``)."""
    out: dict[str, list[str]] = {"credential": [], "document": [], "bgv_package": [], "policy_ack": []}
    for i in items:
        out.setdefault(i.kind, []).append(i.ref)
    return {k: tuple(sorted(set(v))) for k, v in out.items()}

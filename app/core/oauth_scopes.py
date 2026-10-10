# ruff: noqa: E501, D401
"""OAuth scope catalogue (Epic 66, AD-75/AD-76, FR-332). Pure: imports nothing from ``app.services``.

A scope maps to internal ``CATALOG`` permissions (``permissions_for``). Sensitive scopes (``pii.*``, ``documents:read``,
``recordings:read``) need explicit admin consent with a stated purpose and are NEVER in a client-credentials token (D-7)
nor available to API keys (FR-348). ``validate_catalogue`` runs after hooks autodiscovery: a scope mapping a permission
that is unknown, ``NON_DELEGABLE`` or ★ (``STAR_PERMISSIONS``), or a sensitive scope with ``api_key_allowed=True``,
fails startup. Owners register scopes from their own ``hooks.py`` with ``register_scope``.
"""

import dataclasses
import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from app.core.permissions import CATALOG, NON_DELEGABLE, STAR_PERMISSIONS

SCOPE_KEY_RE = re.compile(r"^[a-z][a-z_.]*:(read|write|draft|propose|manage)$")
# Scopes outside the ``<resource>:<op>`` grammar (OIDC-style flags).
SPECIAL_SCOPES = frozenset({"offline_access"})
RESOURCES = frozenset({"api", "mcp"})


@dataclasses.dataclass(frozen=True, slots=True)
class ScopeSpec:
    key: str
    label: str
    description: str  # plain language for the consent screen
    purpose_text: str  # DPDP purpose shown at consent (FR-332)
    permissions: tuple[str, ...]  # internal CATALOG keys this scope can unlock
    sensitive: bool = (
        False  # explicit admin consent with a purpose; NEVER in a client-credentials token (D-7)
    )
    field_groups: frozenset[str] = frozenset()  # P2 field-visibility groups unmasked by this scope (pii.*)
    resources: frozenset[str] = frozenset({"api"})  # api | mcp
    api_key_allowed: bool = True  # False for every sensitive scope (FR-348)
    owner_epic: str = "66"


_scopes: dict[str, ScopeSpec] = {}


def register_scope(spec: ScopeSpec) -> None:
    if spec.key in _scopes:
        msg = f"duplicate scope {spec.key!r}"
        raise ValueError(msg)
    if not (SCOPE_KEY_RE.fullmatch(spec.key) or spec.key in SPECIAL_SCOPES):
        msg = f"invalid scope key {spec.key!r}"
        raise ValueError(msg)
    if not spec.resources or not spec.resources <= RESOURCES:
        msg = f"scope {spec.key}: resources must be a non-empty subset of {sorted(RESOURCES)}"
        raise ValueError(msg)
    _scopes[spec.key] = spec


def unregister_scope(key: str) -> None:
    """Tests only."""
    _scopes.pop(key, None)


def get_scope(key: str) -> ScopeSpec:
    return _scopes[key]


def scopes() -> Mapping[str, ScopeSpec]:
    return MappingProxyType(_scopes)


def is_sensitive(key: str) -> bool:
    spec = _scopes.get(key)
    return spec is not None and spec.sensitive


def permissions_for(scope_keys: Iterable[str]) -> frozenset[str]:
    out: set[str] = set()
    for key in scope_keys:
        spec = _scopes.get(key)
        if spec is not None:
            out.update(spec.permissions)
    return frozenset(out)


def field_groups_for(scope_keys: Iterable[str]) -> frozenset[str]:
    out: set[str] = set()
    for key in scope_keys:
        spec = _scopes.get(key)
        if spec is not None:
            out.update(spec.field_groups)
    return frozenset(out)


def validate_catalogue() -> list[str]:
    problems: list[str] = []
    for spec in _scopes.values():
        for perm in spec.permissions:
            if perm not in CATALOG:
                problems.append(f"scope {spec.key} maps unknown permission {perm}")
            elif perm in NON_DELEGABLE:
                problems.append(f"scope {spec.key} maps the non-delegable permission {perm}")
            elif perm in STAR_PERMISSIONS:
                problems.append(f"scope {spec.key} maps the human-only (★) permission {perm}")
        if spec.sensitive and spec.api_key_allowed:
            problems.append(f"sensitive scope {spec.key} must set api_key_allowed=False")
    return problems


__all__ = [
    "SCOPE_KEY_RE",
    "ScopeSpec",
    "field_groups_for",
    "get_scope",
    "is_sensitive",
    "permissions_for",
    "register_scope",
    "scopes",
    "unregister_scope",
    "validate_catalogue",
]

# ruff: noqa: E501
"""Epic 66 (story 66.10, FR-341): no external (``app``) caller can reach a human-only (★) decision.

* Every route tagged ``public:*`` authorises through ``require_scope`` / the public principal, and no registered scope
  maps a ★, non-delegable or unknown permission (the scope catalogue validation).
* Every tool flagged ``mcp_exposed`` has an MCP exposure and never performs a human-only transition
  (``workflow.catalogue.human_only_transitions``); high / egress tools are exposed only as ``mcp:propose``.
* The ``app`` actor is refused by ``require_human`` and by the P1-P4 ★ guards (the ``app`` probes in the P1-P4
  human-only suites).
* ``PENDING_P5_STAR`` (shrink-only) lists owners' public routes / tools that cannot be exercised yet.
"""

import uuid

import pytest

from app.core import oauth_scopes
from app.core.actors import ActorRef, require_human
from app.core.agentic import tools
from app.core.agentic.exposure import exposures, validate_exposures
from app.core.errors import Forbidden
from app.core.hooks import autodiscover
from app.core.permissions import NON_DELEGABLE, STAR_PERMISSIONS
from app.core.public_api import is_public_route
from app.services.workflow import catalogue

PENDING_P5_STAR: dict[str, str] = {}  # shrink-only; must be empty at phase close


def test_scope_catalogue_never_maps_star_or_non_delegable() -> None:
    autodiscover("app.services")
    assert oauth_scopes.validate_catalogue() == []
    for spec in oauth_scopes.scopes().values():
        assert not set(spec.permissions) & STAR_PERMISSIONS, spec.key
        assert not set(spec.permissions) & NON_DELEGABLE, spec.key
        if spec.sensitive:
            assert not spec.api_key_allowed, spec.key


def test_star_scope_registration_fails_validation() -> None:
    star = next(iter(sorted(STAR_PERMISSIONS)))
    oauth_scopes.register_scope(
        oauth_scopes.ScopeSpec("probe_star:write", "probe", "probe", "probe", (star,), owner_epic="test")
    )
    try:
        assert any("probe_star:write" in p for p in oauth_scopes.validate_catalogue())
    finally:
        oauth_scopes.unregister_scope("probe_star:write")


def test_every_public_route_authorises_through_the_public_principal(app: object) -> None:
    from fastapi.routing import APIRoute

    def deps(route: APIRoute) -> set[str]:
        names: set[str] = set()
        stack = list(route.dependant.dependencies)
        while stack:
            d = stack.pop()
            names.add(getattr(d.call, "__name__", ""))
            stack.extend(d.dependencies)
        return names

    public = [r for r in app.routes if is_public_route(r)]  # type: ignore[attr-defined]
    assert public, "the foundation mounts /api/public/v1/meta"
    for route in public:
        if route.path in PENDING_P5_STAR:
            continue
        names = deps(route)
        assert "public_principal" in names or any(n.startswith("require_scope_") for n in names), route.path


def test_mcp_exposed_tools_never_reach_a_star_transition() -> None:
    autodiscover("app.services")
    assert validate_exposures() == []
    exposed = exposures("mcp")
    for spec in tools().values():
        if not spec.mcp_exposed or spec.key in PENDING_P5_STAR:
            continue
        assert spec.key in exposed, spec.key
        if spec.transition is not None:
            assert not catalogue.is_human_only(*spec.transition), spec.key
        if spec.risk == "high" or spec.egress:
            assert exposed[spec.key].mcp_scope == "mcp:propose", spec.key


@pytest.mark.parametrize("on_behalf", [None, "user"])
def test_app_actor_is_refused_by_require_human(on_behalf: str | None) -> None:
    actor = ActorRef("app", uuid.uuid4(), on_behalf_of=uuid.uuid4() if on_behalf else None)
    with pytest.raises(Forbidden) as exc:
        require_human(actor)
    assert exc.value.code == "human_only_action"


def test_pending_list_is_shrink_only() -> None:
    assert len(PENDING_P5_STAR) <= 0

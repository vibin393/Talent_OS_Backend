"""Edge host -> tenant resolution (Phase 1, Epic 6.2 / AD-1).

Maps a request host (e.g. ``acme.app.example.com`` or ``acme.talentos.localhost``) to a tenant slug,
so a tenant can be reached by its own subdomain and sign-in can infer the tenant from the host instead
of requiring it in the request body. The host only *routes*: it proposes a slug; access is still decided
by auth + the tenant directory, and an unknown/reserved subdomain resolves to ``None`` (neutral).
"""

# Subdomains that are never tenant slugs (platform entrypoints).
_RESERVED = frozenset({"www", "api", "auth", "app", "admin", "static", "cdn", "mail"})


def resolve_tenant_slug_from_host(host: str, root_domain: str) -> str | None:
    """Return the tenant slug encoded in ``host`` under ``root_domain``, or None.

    ``acme.talentos.localhost`` under root ``talentos.localhost`` -> ``"acme"``.
    The bare root, a reserved label, or a multi-label prefix -> None (host only routes).
    """
    if not host or not root_domain:
        return None
    host = host.split(":", 1)[0].strip().lower().rstrip(".")
    root = root_domain.split(":", 1)[0].strip().lower().rstrip(".")
    if host == root or not host.endswith("." + root):
        return None
    prefix = host[: -(len(root) + 1)]  # strip ".<root>"
    if not prefix or "." in prefix or prefix in _RESERVED:
        return None  # only a single, non-reserved label is a tenant slug
    return prefix

"""Data-residency classification of tables (Phase 1, Epic 6.4 / AD-22).

Every table is either **control-plane** — cross-tenant routing/catalog data that lives in the primary
cell and carries no tenant PII (the tenant directory, plans, global catalogs, migration bookkeeping) —
or **tenant-pinned** — tenant business data that lives region-local in that tenant's own database/cell.

The hard invariant (AD-22 / NFR5): **no tenant-PII table may be control-plane** — tenant PII never
leaves the tenant's cell. ``scripts/residency_check.py`` enforces this against the mapped models, so a
new table that would put PII in the control plane fails the check.

Classification reuses ``app.core.tenancy``: platform/infra tables are control-plane; everything else
(the tenant-scoped, RLS/``TenantMixin`` tables) is tenant-pinned.
"""

from app.core import tenancy

CONTROL_PLANE = "control_plane"
TENANT_PINNED = "tenant_pinned"


def residency_of(table_name: str) -> str:
    """Where this table's rows live: the primary control-plane cell, or the tenant's region-local cell."""
    if table_name in tenancy.INFRA_TABLES or table_name in tenancy.PLATFORM_TABLES:
        return CONTROL_PLANE
    return TENANT_PINNED


def is_control_plane(table_name: str) -> bool:
    return residency_of(table_name) == CONTROL_PLANE

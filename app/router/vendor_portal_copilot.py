"""Vendor Portal Copilot (Epic 77, FR-396 / FR-398): ``/api/v1/vendor-portal/copilot``. Vendor session
(``VendorDep``) + ``portal_copilot:use`` on every route except ``GET /status``. FR-80 guarded."""

from app.core.field_visibility import portal_guard_route
from app.dependencies.vendor import require_portal_permission, require_vendor_user
from app.router.portal_copilot_routes import build

router = build(
    "vendor", "/vendor-portal/copilot", require_vendor_user, require_portal_permission("portal_copilot:use"),
    portal_guard_route("vendor_user"),
)  # fmt: skip

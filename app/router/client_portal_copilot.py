# ruff: file-ignore[line-too-long]
"""Client Portal Copilot (Epic 77, FR-396 / FR-397): ``/api/v1/client-portal/copilot``. Client session (``ClientDep``;
the Copilot's tools run on the Client User's org-scoped session) + ``portal_copilot:use`` on every route except
``GET /status``. FR-80 guarded."""

from app.core.field_visibility import portal_guard_route
from app.dependencies.client import require_client_user, require_portal_permission
from app.router.portal_copilot_routes import build

router = build(
    "client", "/client-portal/copilot", require_client_user, require_portal_permission("portal_copilot:use"),
    portal_guard_route("client_user"),
)  # fmt: skip

# ruff: noqa: E501
"""Internal service-to-service API (unified platform), mounted at ``/api/v1/internal``.

NOT a user / tenant-session surface and NOT a Public API route: it is called backend-to-backend on the compose
network only. Every call must carry ``X-Internal-Secret`` equal to ``settings.internal_service_secret``
(constant-time compare); a missing / empty configured secret or a mismatch is 401 (fail closed). Kept out of
the OpenAPI document (``include_in_schema=False``) and off the module gateway.

``GET /tenant-ats-key?tenant_id=<uuid>`` returns the CALLING tenant's own ATS Public API key (find-or-mint), so
the Portal / Croar / Feedback IQ spokes push each tenant's data only into that tenant's ATS silo.
"""

import hmac
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query

from app.core.errors import Unauthorized
from app.core.settings import get_settings
from app.services import internal_integration_service as svc

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


def _require_internal_secret(provided: str | None) -> None:
    configured = get_settings().internal_service_secret
    if not configured or not provided or not hmac.compare_digest(provided, configured):
        raise Unauthorized("Invalid or missing internal service secret", code="invalid_internal_secret")


@router.get("/tenant-ats-key", response_model=None)
async def tenant_ats_key(
    tenant_id: Annotated[uuid.UUID, Query()],
    x_internal_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Return ``{"api_key", "base_url", "owner_id", "client_id"}`` for the tenant's own ATS integration
    (service-to-service only). ``owner_id`` / ``client_id`` are the in-tenant user and default Client that spoke
    creates file records under (``owner_id`` null when the tenant has no users)."""
    _require_internal_secret(x_internal_secret)
    return await svc.get_or_mint_ats_key(tenant_id)

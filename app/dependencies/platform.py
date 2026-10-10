"""Platform operator dependency for Epic 12 operator endpoints (``/api/v1/platform/*``).

``require_platform_operator`` (identity: ``tos_platform`` cookie + CSRF) plus MFA: the operator must have
enrolled a second factor (story 1.4) unless ``PLATFORM_OPERATOR_MFA_REQUIRED=false``; else 403
``mfa_enrollment_required``. The operator console never uses tenant cookies.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.core.database import platform_session
from app.core.errors import Forbidden
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.models.identity import PlatformOperator
from app.services import auth_service as identity_service


async def require_platform_operator_mfa(request: Request) -> Principal:
    principal = await identity_service.require_platform_operator(request)
    if get_settings().platform_operator_mfa_required:
        async with platform_session() as pdb:
            operator = await pdb.get(PlatformOperator, principal.id)
            enrolled = operator is not None and operator.mfa_enabled
        if not enrolled:
            raise Forbidden("Enrol in multi-factor authentication first", code="mfa_enrollment_required")
    return principal


PlatformOperatorMfaDep = Annotated[Principal, Depends(require_platform_operator_mfa)]

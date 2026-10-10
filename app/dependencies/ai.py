"""AI feature toggle dependency (Epic 12 story 12.4, FR-62).

``Depends(require_ai_feature("matching"))`` refuses the route with 403 ``ai_feature_disabled`` when the
tenant turned the feature off. The gateway refuses disabled features again as a second layer; manual paths
(non-AI routes) stay available.
"""

from collections.abc import Awaitable, Callable

from app.core import rate_limit, rate_limit_policies
from app.core.errors import Forbidden
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.models.ai import AI_FEATURES
from app.services import ai


def require_ai_feature(feature: str) -> Callable[..., Awaitable[None]]:
    if feature not in AI_FEATURES:
        msg = f"unknown AI feature {feature!r}"
        raise ValueError(msg)

    async def dependency(principal: CurrentUserDep, db: TenantDBSessionDep) -> None:
        if not await ai.is_feature_enabled(db, feature):
            raise Forbidden(
                "This AI feature is turned off for your workspace",
                code="ai_feature_disabled",
                details={"feature": feature},
            )
        # NFR-16: interactive AI is limited per user (bulk work is paced by the fairness semaphore).
        await rate_limit.enforce("ai_interactive", str(principal.id), *rate_limit_policies.AI_INTERACTIVE_LIMIT)

    dependency.__name__ = f"require_ai_feature_{feature}"
    return dependency

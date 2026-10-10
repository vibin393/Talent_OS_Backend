"""Named approval routes (Epic 13, R5): a domain registers a resolver that builds the explicit steps of a
route (e.g. Epic 25's ``placement_time_route``), other domains call ``resolve_route`` without importing it."""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.registry import Registry
from app.services.approvals.registry import StepSpec

RouteResolver = Callable[..., Awaitable[list[StepSpec]]]

_routes: Registry[str, RouteResolver] = Registry("approval route")


def register_route_resolver(name: str, fn: RouteResolver) -> None:
    _routes.register(name, fn)


async def resolve_route(ctx: RequestContext, db: AsyncSession, name: str, **kwargs: Any) -> list[StepSpec]:
    fn = _routes.find(name)
    if fn is None:
        raise ValidationFailed("Unknown approval route", code="unknown_route", details={"route": name})
    return list(await fn(ctx, db, **kwargs))

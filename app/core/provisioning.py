"""Tenant provisioning steps (Epic 13). Domains register what a new tenant needs (default Legal Entity + open
periods, retention classes, ...) from their ``hooks.py``; ``auth_service.create_tenant`` runs every step in the
provisioning transaction (identity never imports a Phase 2 domain, AD-32)."""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.registry import Registry

TenantProvisioner = Callable[[AsyncSession, Any], Awaitable[Any]]

_provisioners: Registry[str, TenantProvisioner] = Registry("tenant provisioner")


def register_tenant_provisioner(name: str, fn: TenantProvisioner, *, order: int = 100) -> None:
    _provisioners.register(f"{order:04d}:{name}", fn)


async def provision_tenant(db: AsyncSession, tenant: Any) -> list[str]:
    """Run every registered step (in order) inside ``db``'s transaction; returns the step names."""
    from app.core.hooks import autodiscover

    autodiscover("app.services")
    done: list[str] = []
    for key, fn in sorted(_provisioners.view().items()):
        await fn(db, tenant)
        done.append(key.split(":", 1)[1])
    return done

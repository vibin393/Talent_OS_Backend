"""Registrations of the ``privacy`` domain (autodiscovered by ``core.hooks``; Epic 13): seed the default
retention classes for every new tenant (AD-34)."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.provisioning import register_tenant_provisioner


async def _seed_retention_classes(db: AsyncSession, tenant: Any) -> None:  # ruff: ignore[unused-function-argument]
    from app.services.privacy.retention_classes import seed_defaults, seed_system_classes

    await seed_defaults(db)
    await seed_system_classes(db)  # Epic 35: anchors, floors, expiry actions + Phase 2 seeds


register_tenant_provisioner("privacy.retention_classes", _seed_retention_classes, order=20)

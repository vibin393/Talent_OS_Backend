# ruff: noqa: E501
"""Registrations of the ``recording_policy`` domain (autodiscovered by ``core.hooks``), Epic 53: seed the default
Recording Policy (``IN`` / ``candidate_consent``) for every new tenant (coordinator decision 9)."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.provisioning import register_tenant_provisioner


async def _seed_recording_policy(db: AsyncSession, tenant: Any) -> None:  # noqa: ARG001
    from app.services.recording_policy.gate import seed_tenant_defaults

    await seed_tenant_defaults(db)


register_tenant_provisioner("recording_policy.defaults", _seed_recording_policy, order=25)

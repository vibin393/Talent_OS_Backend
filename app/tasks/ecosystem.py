# ruff: noqa: E501
"""Ecosystem maintenance tasks (Epic 66), queue ``maintenance``: OAuth lookup purge, exposure flush, monthly partitions,
vault rewrap, log purges and idle external (MCP) session close."""

QUEUE = "maintenance"


# ---------------------------------------------------------- Epic 66 (D-11): subject exposure buffer flush
from app.core.celery import celery_app  # noqa: E402
from app.tasks.runtime import run  # noqa: E402


@celery_app.task(name="exposure.flush", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def exposure_flush() -> int:
    """Beat ``exposure-flush`` (every 60 s): upsert the buffered ``subject_exposures``."""

    async def job() -> int:
        from app.services import exposure_service

        return await exposure_service.flush()

    return int(run(job))


# ---------------------------------------------------------- Epic 66 (AD-76): expired OAuth token lookups (hourly)
@celery_app.task(name="oauth.purge_lookups", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def oauth_purge_lookups() -> int:
    async def job() -> int:
        from app.services import oauth_service

        return await oauth_service.purge_lookups()

    return int(run(job))


# ---------------------------------------------------------- Epic 66 (AD-82): master-key rewrap (operator, on demand)
@celery_app.task(name="vault.rewrap", queue=QUEUE)  # type: ignore[misc,untyped-decorator,unused-ignore]
def vault_rewrap(tenant_id: str) -> int:
    async def job() -> int:
        import uuid

        from app.services import vault_service

        return await vault_service.rewrap(uuid.UUID(str(tenant_id)))

    return int(run(job))

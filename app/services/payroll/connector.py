"""Payroll provider selection per Legal Entity (Epic 28 A7, AD-29; ``payroll:configure``).

Phase 2 ships the ``manual`` provider (download -> Mark Processed); ``fake`` exists outside production for
tests. Selection writes ``provider_connector_configs`` through the connector framework.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.connectors import ConnectorConfigIn, ConnectorConfigUpdate
from app.schemas.payroll import PayrollConnectorIn, PayrollConnectorOut
from app.services.connectors import registry
from app.services.payroll.pay_periods import legal_entity


async def get_connector(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID
) -> PayrollConnectorOut:
    del ctx
    await legal_entity(db, legal_entity_id)
    row = await registry.active_config(db, "payroll", legal_entity_id)
    return PayrollConnectorOut(
        legal_entity_id=legal_entity_id,
        provider_key=row.provider_key if row is not None else "manual",
        config_id=row.id if row is not None else None,
    )


async def put_connector(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, data: PayrollConnectorIn
) -> PayrollConnectorOut:
    from app.services import connectors_service

    await legal_entity(db, legal_entity_id)
    row = await registry.active_config(db, "payroll", legal_entity_id)
    if row is not None and row.provider_key == data.provider_key and row.legal_entity_id == legal_entity_id:
        return await get_connector(ctx, db, legal_entity_id)
    if row is not None and row.legal_entity_id == legal_entity_id:
        await connectors_service.update_config(ctx, db, row.id, ConnectorConfigUpdate(status="disabled"))
    await connectors_service.create_config(
        ctx,
        db,
        ConnectorConfigIn(
            category="payroll",
            provider_key=data.provider_key,
            legal_entity_id=legal_entity_id,
            display_name=f"Payroll ({data.provider_key})",
        ),
    )
    return await get_connector(ctx, db, legal_entity_id)

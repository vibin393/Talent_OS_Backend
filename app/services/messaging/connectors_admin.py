# ruff: file-ignore[line-too-long]
"""SMS / WhatsApp connector administration (Epic 33 A4; ``messaging:configure``): a per-category wrapper of the
foundation config CRUD (credentials are write-only) plus "Send test"."""

import dataclasses
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.connectors import ProviderConnectorConfig
from app.schemas.connectors import ConnectorConfigIn, ConnectorConfigOut, ConnectorConfigUpdate
from app.schemas.messaging import MessagingConnectorIn, MessagingConnectorUpdate, StatusOut
from app.services.connectors import registry
from app.services.connectors import service as config_service
from app.services.connectors.base import ManualActionRequired
from app.services.connectors.sms.channel import SendSpec
from app.services.connectors.sms.port import PermanentSendError

CATEGORIES = ("sms", "whatsapp")


class MessagingConnectorsOut(BaseModel):
    category: str
    providers: list[str]
    configs: list[ConnectorConfigOut]


async def list_connectors(ctx: RequestContext, db: AsyncSession, category: str) -> MessagingConnectorsOut:
    configs = [c for c in await config_service.list_configs(ctx, db) if c.category == category]
    return MessagingConnectorsOut(
        category=category, providers=registry.provider_keys(category), configs=configs
    )


async def create_connector(
    ctx: RequestContext, db: AsyncSession, data: MessagingConnectorIn
) -> ConnectorConfigOut:
    return await config_service.create_config(ctx, db, ConnectorConfigIn.model_validate(data.model_dump()))


async def _own(db: AsyncSession, config_id: uuid.UUID) -> ProviderConnectorConfig:
    row = await db.get(ProviderConnectorConfig, config_id)
    if row is None or row.category not in CATEGORIES:
        raise NotFound("Connector configuration not found")
    return row


async def update_connector(
    ctx: RequestContext, db: AsyncSession, config_id: uuid.UUID, data: MessagingConnectorUpdate
) -> ConnectorConfigOut:
    await _own(db, config_id)
    return await config_service.update_config(
        ctx, db, config_id, ConnectorConfigUpdate.model_validate(data.model_dump(exclude_unset=True))
    )


async def send_test(ctx: RequestContext, db: AsyncSession, config_id: uuid.UUID, to_e164: str) -> StatusOut:  # ruff: ignore[unused-function-argument]
    row = await _own(db, config_id)
    adapter: Any = registry.build(row.category, await registry.load_config(db, row))
    spec = SendSpec(
        channel=row.category,
        to_e164=to_e164,
        body="Talent OS test message",
        template_ref=str(
            (row.settings or {}).get("test_template")
            or ("hello_world" if row.category == "whatsapp" else "0" * 19)
        ),
        variables=[],
    )
    try:
        result = await adapter.handle("send_test", {"spec": dataclasses.asdict(spec)})
    except ManualActionRequired:
        return StatusOut(status="manual")
    except PermanentSendError as exc:
        return StatusOut(status=f"failed:{exc.code}")
    except Exception as exc:  # provider down / misconfigured: report, never raise the provider's text
        return StatusOut(status=f"failed:{type(exc).__name__}")
    return StatusOut(status=str(result.get("status") or "sent"))

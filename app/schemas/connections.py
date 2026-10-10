"""Calendar / mailbox connection API models (Epic 7, story 7.4). Tokens never appear in any response."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConnectionProvider = Literal["google", "microsoft"]
ConnectionStatus = Literal["active", "needs_reconnect", "revoked"]


class ProviderOut(BaseModel):
    provider: ConnectionProvider
    configured: bool
    scopes: list[str]


class StartOut(BaseModel):
    authorize_url: str


class ConnectionOut(BaseModel):
    id: uuid.UUID
    provider: str
    account_email: str | None
    scopes: list[str]
    status: ConnectionStatus
    connected_at: datetime
    expires_at: datetime | None
    last_error: str | None


class FreeBusyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    start_utc: datetime
    end_utc: datetime


class BusyOut(BaseModel):
    start: datetime
    end: datetime


class UserFreeBusyOut(BaseModel):
    user_id: uuid.UUID
    status: Literal["available", "unknown"]
    busy: list[BusyOut]
    source: Literal["personal", "organisation"] | None = None  # Epic 72 (additive): who supplied the busy times


class FreeBusyOut(BaseModel):
    items: list[UserFreeBusyOut]

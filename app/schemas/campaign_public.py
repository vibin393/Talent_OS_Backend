"""Public marketing link DTOs (Epic 39, ``/api/v1/public/m/{token}/*``). No personal data is ever returned."""

from pydantic import BaseModel, ConfigDict


class UnsubscribeView(BaseModel):
    tenant_name: str
    channel: str
    already_unsubscribed: bool


class UnsubscribeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    all: bool = False


class UnsubscribeOut(BaseModel):
    status: str
    channel: str
    all: bool

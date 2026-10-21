"""Schemas for the per-tenant outbound email (SMTP) settings admin API.

The password is write-only: it is accepted on PUT (``smtp_password``; omitted or null keeps the stored one)
and never serialised back (``MailSettingsOut`` exposes only ``password_set``).
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator

MailBackend = Literal["platform", "smtp"]

_Address = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
_Username = Annotated[str, StringConstraints(strip_whitespace=True, max_length=320)]
_Password = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
_SenderName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
_SenderEmail = Annotated[EmailStr, StringConstraints(max_length=320)]
_Port = Annotated[int, Field(ge=1, le=65535)]


class MailSettingsOut(BaseModel):
    """Current tenant mail config. The password is never returned — only whether one is stored."""

    model_config = ConfigDict(from_attributes=True)

    backend: MailBackend
    smtp_address: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_use_ssl: bool
    sender_email: str | None
    sender_name: str | None
    enabled: bool
    password_set: bool


class MailSettingsUpdate(BaseModel):
    """Upsert the tenant mail config. ``smtp_password`` is write-only: omit it (or send null) to keep the
    currently stored password."""

    backend: MailBackend = "platform"
    smtp_address: _Address | None = None
    smtp_port: _Port = 587
    smtp_username: _Username | None = None
    smtp_password: _Password | None = None
    smtp_use_ssl: bool = False
    sender_email: _SenderEmail | None = None
    sender_name: _SenderName | None = None
    enabled: bool = False

    @model_validator(mode="after")
    def _require_smtp_fields(self) -> Self:
        if self.backend == "smtp" and not (self.smtp_address and self.sender_email):
            msg = "backend 'smtp' requires smtp_address and sender_email"
            raise ValueError(msg)
        return self


class MailTestRequest(BaseModel):
    to: _SenderEmail


class MailTestResult(BaseModel):
    ok: bool
    channel: str  # "tenant_smtp" | "platform" | "outbox"
    error: str | None = None


__all__ = ["MailBackend", "MailSettingsOut", "MailSettingsUpdate", "MailTestRequest", "MailTestResult"]

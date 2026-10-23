"""Per-tenant outbound email (SMTP) settings: read, upsert (password encrypted at rest) and a live test send.

The password is encrypted with ``app.core.security`` (``connector`` purpose — the same Fernet mechanism
provider-connector credentials use) and never returned. Every write invalidates the per-tenant mailer cache
in ``app.core.mailer`` so the next send picks up the new config.
"""

import asyncio
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import mailer as mailer_mod
from app.core.context import RequestContext
from app.core.mailer import EmailMessage
from app.core.security import encrypt_str
from app.models.mail_settings import DEFAULT_SMTP_PORT, TenantMailSettings
from app.schemas.mail_settings import MailSettingsOut, MailSettingsUpdate, MailTestResult

# Reuse the connector credential key (``CONNECTOR_ENCRYPTION_KEY``; dev key outside production).
SECRET_PURPOSE = "connector"
TEST_SUBJECT = "Talent OS test email"
TEST_BODY = (
    "This is a test email from Talent OS, sent to verify your organization's outbound email settings.\n\n"
    "If you received this message, your mail configuration is working."
)


async def _row(db: AsyncSession, tenant_id: uuid.UUID) -> TenantMailSettings | None:
    return (
        await db.execute(select(TenantMailSettings).where(TenantMailSettings.tenant_id == tenant_id))
    ).scalar_one_or_none()


def _to_out(row: TenantMailSettings | None) -> MailSettingsOut:
    if row is None:
        return MailSettingsOut(
            backend="platform",
            smtp_address=None,
            smtp_port=DEFAULT_SMTP_PORT,
            smtp_username=None,
            smtp_use_ssl=False,
            sender_email=None,
            sender_name=None,
            enabled=False,
            password_set=False,
        )
    return MailSettingsOut(
        backend=row.backend,
        smtp_address=row.smtp_address,
        smtp_port=row.smtp_port,
        smtp_username=row.smtp_username,
        smtp_use_ssl=row.smtp_use_ssl,
        sender_email=row.sender_email,
        sender_name=row.sender_name,
        enabled=row.enabled,
        password_set=bool(row.smtp_password_encrypted),
    )


async def get_mail_settings(db: AsyncSession, tenant_id: uuid.UUID) -> MailSettingsOut:
    return _to_out(await _row(db, tenant_id))


async def upsert_mail_settings(
    ctx: RequestContext, db: AsyncSession, body: MailSettingsUpdate
) -> MailSettingsOut:
    tenant_id = ctx.tenant_id
    assert tenant_id is not None  # tenant-scoped route
    row = await _row(db, tenant_id)
    if row is None:
        row = TenantMailSettings(tenant_id=tenant_id)
        db.add(row)
    row.backend = body.backend
    row.smtp_address = body.smtp_address
    row.smtp_port = body.smtp_port
    row.smtp_username = body.smtp_username
    row.smtp_use_ssl = body.smtp_use_ssl
    row.sender_email = body.sender_email
    row.sender_name = body.sender_name
    row.enabled = body.enabled
    # Write-only password: set only when provided; omit/null keeps the stored ciphertext.
    if "smtp_password" in body.model_fields_set and body.smtp_password is not None:
        row.smtp_password_encrypted = encrypt_str(SECRET_PURPOSE, body.smtp_password)  # type: ignore[arg-type]
    row.updated_by = getattr(ctx.principal, "id", None)
    await db.flush()
    out = _to_out(row)
    mailer_mod.invalidate_tenant_mailer(tenant_id)  # next send resolves the new config
    return out


async def send_test_email(tenant_id: uuid.UUID, to: str) -> MailTestResult:
    """Send a real test email through the tenant's resolved mailer and report the channel used."""
    mailer_mod.invalidate_tenant_mailer(tenant_id)  # always test the freshest config
    message = EmailMessage(
        to=to, subject=TEST_SUBJECT, text=TEST_BODY, tenant_id=tenant_id, kind="mail_settings_test"
    )
    mailer, channel = await mailer_mod.resolve_mailer(message)
    try:
        await asyncio.to_thread(mailer.send, message)
    except Exception as exc:
        logger.warning(
            "Mail settings test send failed (tenant_id={}, error={})", tenant_id, type(exc).__name__
        )
        return MailTestResult(ok=False, channel=channel, error=f"{type(exc).__name__}: {exc}")
    return MailTestResult(ok=True, channel=channel, error=None)


__all__ = ["SECRET_PURPOSE", "get_mail_settings", "send_test_email", "upsert_mail_settings"]

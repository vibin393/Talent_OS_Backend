"""Outgoing email port.

Services build an ``EmailMessage`` and call ``get_mailer().send(message)``. Backends (``MAILER_BACKEND``):

* ``outbox`` (dev/test): logs that a message was sent (no recipient or body -- they may hold PII or secrets
  such as invitation links) and writes the full message as JSON to ``Settings.outbox_dir``
  (default ``Backend/data/outbox/``).
* ``smtp``: delivers through an SMTP server (e.g. Gmail + app password); in dev it also keeps an outbox copy.
"""

import asyncio
import base64
import dataclasses
import json
import smtplib
import ssl
import threading
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid, parseaddr
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from app.core.settings import get_settings


@dataclasses.dataclass(frozen=True, slots=True)
class EmailAttachment:
    """Epic 6 (additive): a file attached to a message (e.g. the submission resume snapshot)."""

    filename: str
    content_type: str
    data: bytes


@dataclasses.dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None
    tenant_id: uuid.UUID | None = None
    kind: str = "generic"  # e.g. "tenant_admin_invitation"; lets tests and ops filter messages
    id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    created_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))
    # Epic 10 (additive): copies, reply-to, extra headers (e.g. ``X-TOS-Message-Id``) and the display name
    # of the sender ("<User> via <Tenant>"); the address itself stays the platform sender.
    cc: tuple[str, ...] = ()
    reply_to: str | None = None
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    from_name: str | None = None
    # Epic 6 (additive): attachments (the outbox stores metadata + base64).
    attachments: tuple[EmailAttachment, ...] = ()
    # Epic 7 (additive): send through this user's connected mailbox when possible (``RoutingMailer``).
    sender_user_id: uuid.UUID | None = None


class Mailer(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class OutboxMailer:
    """Writes each message to ``<directory>/<timestamp>_<id>.json``; the last ``SENT_MAXLEN`` messages
    are kept in ``self.sent`` for tests. Dev/test only (refused in production by ``get_mailer``)."""

    SENT_MAXLEN = 1000

    def __init__(self, directory: Path, sender: str) -> None:
        self.directory = directory
        self.sender = sender
        self.sent: deque[EmailMessage] = deque(maxlen=self.SENT_MAXLEN)
        self._lock = threading.Lock()

    def _payload(self, message: EmailMessage) -> dict[str, Any]:
        return {
            "id": str(message.id),
            "created_at": message.created_at.isoformat().replace("+00:00", "Z"),
            "kind": message.kind,
            "tenant_id": str(message.tenant_id) if message.tenant_id else None,
            "from": self.sender,
            "to": message.to,
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
            "cc": list(message.cc),
            "reply_to": message.reply_to,
            "headers": dict(message.headers),
            "from_name": message.from_name,
            "sender_user_id": str(message.sender_user_id) if message.sender_user_id else None,
            "attachments": [
                {
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size": len(a.data),
                    "data_base64": base64.b64encode(a.data).decode("ascii"),
                }
                for a in message.attachments
            ],
        }

    def send(self, message: EmailMessage) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = message.created_at.strftime("%Y%m%dT%H%M%S%f")
        path = self.directory / f"{stamp}_{message.id}.json"
        path.write_text(json.dumps(self._payload(message), indent=2), encoding="utf-8")
        with self._lock:
            self.sent.append(message)
        logger.info(
            "Email written to outbox (id={}, kind={}, tenant_id={}, file={})",
            message.id,
            message.kind,
            message.tenant_id,
            path.name,
        )

    def outbox_path(self, message: EmailMessage) -> Path | None:
        matches = list(self.directory.glob(f"*_{message.id}.json"))
        return matches[0] if matches else None


class MailerConfigError(RuntimeError):
    pass


class SmtpMailer:
    """Delivers messages through an SMTP server (STARTTLS, or implicit TLS with ``use_ssl``).

    Logs only the message id and kind -- recipients and bodies may hold PII or secret links. An optional
    ``copy_to`` mailer (the dev outbox) receives a copy after a successful send.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        *,
        use_ssl: bool = False,
        timeout: float = 30.0,
        copy_to: Mailer | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.copy_to = copy_to

    def build(self, message: EmailMessage) -> MimeMessage:
        display, address = parseaddr(self.sender)
        mime = MimeMessage()
        mime["From"] = formataddr((message.from_name or display or "", address))
        mime["To"] = message.to
        if message.cc:
            mime["Cc"] = ", ".join(message.cc)
        if message.reply_to:
            mime["Reply-To"] = message.reply_to
        mime["Subject"] = message.subject
        mime["Message-ID"] = make_msgid(idstring=str(message.id), domain=address.rpartition("@")[2] or None)
        for name, value in message.headers.items():
            mime[name] = value
        mime.set_content(message.text or "")
        if message.html:
            mime.add_alternative(message.html, subtype="html")
        for att in message.attachments:
            maintype, _, subtype = (att.content_type or "application/octet-stream").partition("/")
            mime.add_attachment(
                att.data, maintype=maintype, subtype=subtype or "octet-stream", filename=att.filename
            )
        return mime

    def send(self, message: EmailMessage) -> None:
        mime = self.build(message)
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if self.use_ssl:
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with smtp:
            if not self.use_ssl:
                smtp.starttls(context=context)
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(mime)
        logger.info(
            "Email sent via SMTP (id={}, kind={}, tenant_id={})", message.id, message.kind, message.tenant_id
        )
        if self.copy_to is not None:
            try:
                self.copy_to.send(message)
            except Exception:
                logger.exception("Could not write the outbox copy (id={})", message.id)


@lru_cache
def get_mailer() -> Mailer:
    s = get_settings()
    if s.mailer_backend == "smtp":
        if not s.smtp_address:
            msg = "MAILER_BACKEND=smtp requires SMTP_ADDRESS"
            raise MailerConfigError(msg)
        copy = None
        if s.mailer_outbox_copy and s.app_env != "production":
            copy = OutboxMailer(Path(s.outbox_dir), s.effective_mail_from)
        return SmtpMailer(
            s.smtp_address,
            s.smtp_port,
            s.smtp_username,
            s.smtp_password,
            s.effective_mail_from,
            use_ssl=s.smtp_use_ssl,
            timeout=float(s.smtp_timeout_seconds),
            copy_to=copy,
        )
    if s.app_env == "production":
        msg = "MAILER_BACKEND=outbox is dev/test only and must not be used in production"
        raise MailerConfigError(msg)
    return OutboxMailer(Path(s.outbox_dir), s.effective_mail_from)


async def send_email(message: EmailMessage) -> None:
    """Send from async code without blocking the event loop (mailers do blocking I/O)."""
    mailer = get_mailer()
    if await _sandbox_message(message):  # Epic 66 (FR-339): a Sandbox Tenant's mail only ever reaches the outbox
        s = get_settings()
        mailer = OutboxMailer(Path(s.outbox_dir), s.effective_mail_from)
    await asyncio.to_thread(mailer.send, message)


async def _sandbox_message(message: EmailMessage) -> bool:
    """True when the message's tenant (``message.tenant_id``, else the context tenant) is a Sandbox Tenant."""
    from app.core.context import get_context
    from app.core.tenancy import is_sandbox

    tenant_id = message.tenant_id or get_context().tenant_id
    if tenant_id is None:
        return False
    try:
        return await is_sandbox(tenant_id)
    except Exception:  # never break sending on the mode lookup
        logger.exception("Sandbox mode lookup failed (tenant_id={})", tenant_id)
        return False


# ================================================================ mailbox routing (Epic 7, FR-53)
# ``(tenant_id, user_id, message) -> sent?`` -- installed by ``connections_service`` (core never imports
# services, AD-1). False / an exception means "no usable mailbox": the platform mailer sends instead.
MailboxSender = Callable[[uuid.UUID, uuid.UUID, EmailMessage], Awaitable[bool]]
_mailbox_sender: MailboxSender | None = None


def set_mailbox_sender(sender: MailboxSender | None) -> None:
    global _mailbox_sender  # process-wide wiring, set once at import
    _mailbox_sender = sender


class RoutingMailer:
    """Sends a message with ``sender_user_id`` through that user's connected Google / Microsoft mailbox and
    falls back to the platform mailer (``get_mailer()``) otherwise. Returns the channel used."""

    async def send(self, message: EmailMessage) -> str:
        sender = _mailbox_sender
        if (
            sender is not None
            and message.sender_user_id is not None
            and message.tenant_id is not None
            and not await _sandbox_message(message)  # Epic 66: no real mailbox sends from a Sandbox Tenant
        ):
            try:
                if await sender(message.tenant_id, message.sender_user_id, message):
                    logger.info("Email sent through the user's mailbox (id={}, kind={})", message.id, message.kind)
                    return "mailbox"
            except Exception as exc:
                logger.warning("Mailbox send failed, using the platform mailer (id={}, error={})", message.id, type(exc).__name__)
        await send_email(message)
        return "platform"


async def send_routed(message: EmailMessage) -> str:
    """``RoutingMailer().send(message)`` -- ``"mailbox"`` or ``"platform"``."""
    return await RoutingMailer().send(message)

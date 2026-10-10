"""``CalendarProvider`` port (Epic 7, story 7.4, FR-53) and its value types.

Implementations: ``google.GoogleProvider`` (Calendar v3 + Gmail), ``microsoft.MicrosoftProvider`` (Graph) and
``fake.FakeProvider`` (in-memory, tests / dev). Every HTTP call uses ``httpx.AsyncClient`` with a 10 s timeout;
tests never call real APIs. Token values never appear in logs, errors or API responses.
"""

import dataclasses
from datetime import datetime
from typing import Literal, Protocol

from app.core.mailer import EmailMessage

ProviderName = Literal["google", "microsoft", "fake"]
HTTP_TIMEOUT_SECONDS = 10.0

GOOGLE_SCOPES: tuple[str, ...] = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/gmail.send",
)
MICROSOFT_SCOPES: tuple[str, ...] = ("offline_access", "openid", "email", "Calendars.ReadWrite", "Mail.Send")

# Epic 33 (FR-171): opt-in reply-capture scopes, requested only by ``POST /connections/{provider}/enable-reply-
# capture``. The send-only sets above are unchanged. Gmail ``readonly`` is a restricted scope (Google
# verification needed before production use).
GOOGLE_REPLY_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)
MICROSOFT_REPLY_SCOPES: tuple[str, ...] = ("Mail.ReadBasic", "Mail.Read")
FAKE_REPLY_SCOPES: tuple[str, ...] = ("mail.read",)
REPLY_SCOPES: dict[str, tuple[str, ...]] = {
    "google": GOOGLE_REPLY_SCOPES,
    "microsoft": MICROSOFT_REPLY_SCOPES,
    "fake": FAKE_REPLY_SCOPES,
}


def has_reply_scopes(
    provider: str, scopes: "tuple[str, ...] | list[str] | None", impl_name: str | None = None
) -> bool:
    """True when ``scopes`` include every reply-capture scope of the provider (the fake provider stands in for
    both real ones in dev / tests)."""
    granted = {s.lower() for s in scopes or ()}
    for name in dict.fromkeys((provider, impl_name or provider, "fake")):
        wanted = REPLY_SCOPES.get(name)
        if wanted and all(w.lower() in granted for w in wanted):
            return True
    return False


class ProviderError(Exception):
    """A provider call failed (network, 5xx, unexpected payload). The message never holds token values."""


class ProviderAuthError(ProviderError):
    """The provider refused the credentials (401 / invalid_grant): the connection needs reconnecting."""


# Epic 10's comms ``deliver`` falls back to the platform mailer on an exception of this name.
class ConnectionExpired(ProviderAuthError):  # noqa: N818 - name fixed by the Epic 10 interface
    pass


class SyncTokenInvalid(ProviderError):  # noqa: N818 - name fixed by the Epic 72 spec
    """Epic 72: the provider invalidated the delta / sync token (Google 410, Graph ``syncStateNotFound``)."""


@dataclasses.dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: tuple[str, ...] = ()
    account_email: str | None = None

    def __repr__(self) -> str:  # never print token values
        return f"TokenSet(expires_at={self.expires_at!r}, scopes={len(self.scopes)})"


@dataclasses.dataclass(frozen=True, slots=True)
class Attendee:
    email: str
    name: str | None = None
    optional: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class EventSpec:
    ical_uid: str
    summary: str
    description: str
    start_utc: datetime
    end_utc: datetime
    timezone: str
    attendees: tuple[Attendee, ...]
    location: str | None = None
    meeting_url: str | None = None
    create_online_meeting: bool = False
    provider_event_id: str | None = None  # set when updating an existing event
    sequence: int = 0
    tos_tag: str | None = None  # Epic 72: version tag (private extended property); None = P1 behaviour


@dataclasses.dataclass(frozen=True, slots=True)
class EventResult:
    provider_event_id: str
    meeting_url: str | None = None
    etag: str | None = None  # Epic 72: provider etag / changeKey after the write


@dataclasses.dataclass(frozen=True, slots=True)
class BusyBlock:
    start: datetime
    end: datetime


@dataclasses.dataclass(frozen=True, slots=True)
class SentRef:
    """Epic 33: what a mailbox send returns -- the provider's message id, its thread / conversation id and the
    RFC 5322 Message-ID when known (used to capture replies to Talent OS-originated threads only)."""

    provider_message_id: str | None
    thread_ref: str | None = None
    rfc_message_id: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message of a captured thread (Epic 33). Attachment bytes are never fetched (names only)."""

    provider_message_id: str
    rfc_message_id: str | None
    in_reply_to: str | None
    from_address: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    sent_at: datetime
    body_text: str
    body_html: str | None
    attachment_names: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderMessage:
    """Epic 55 (coordinator decision C-3): one message of the Demand Agent intake folder / label -- the raw
    RFC 822 bytes (parsed, scanned and stored by ``demand_intake``) plus the provider id and receive time."""

    provider_message_id: str
    rfc_message_id: str | None
    received_at: datetime
    raw: bytes

    def __repr__(self) -> str:  # never print message content
        return f"ProviderMessage(id={self.provider_message_id!r}, bytes={len(self.raw)})"


# ---------------------------------------------------------------- Epic 72: calendar sync (watch + delta)
@dataclasses.dataclass(frozen=True, slots=True)
class ChannelRef:
    """A push channel: our UUID (text), Google ``resourceId`` / Graph subscription id, provider expiry."""

    channel_id: str
    resource_id: str | None
    expires_at: datetime | None

    def __repr__(self) -> str:
        return f"ChannelRef(channel_id={self.channel_id!r}, expires_at={self.expires_at!r})"


ResponseKind = Literal["needs_action", "accepted", "tentative", "declined"]


@dataclasses.dataclass(frozen=True, slots=True)
class AttendeeResponse:
    email: str
    response: ResponseKind
    is_self: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class EventChange:
    """One change of a Talent OS-linked event (every other event is dropped inside ``list_changes``)."""

    provider_event_id: str
    ical_uid: str | None
    status: Literal["confirmed", "tentative", "cancelled", "deleted"]
    start_utc: datetime | None
    end_utc: datetime | None
    etag: str | None
    tos_tag: str | None
    attendees: tuple[AttendeeResponse, ...] = ()
    organizer_is_self: bool = True


class CalendarProvider(Protocol):
    name: ProviderName
    scopes: tuple[str, ...]

    def authorize_url(self, state: str, redirect_uri: str, extra_scopes: tuple[str, ...] = ()) -> str: ...

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet: ...

    async def refresh(self, token_set: TokenSet) -> TokenSet: ...

    async def revoke(self, token_set: TokenSet) -> None: ...

    async def upsert_event(self, token_set: TokenSet, event: EventSpec) -> EventResult: ...

    async def cancel_event(self, token_set: TokenSet, provider_event_id: str) -> None: ...

    async def free_busy(
        self, token_set: TokenSet, emails: list[str], start_utc: datetime, end_utc: datetime
    ) -> dict[str, list[BusyBlock]]: ...

    async def send_mail(self, token_set: TokenSet, message: EmailMessage) -> "SentRef | str | None": ...

    # Epic 33: read ONE thread started by Talent OS (never list or search the mailbox).
    async def list_thread_messages(
        self, token_set: TokenSet, thread_ref: str, since_ref: str | None
    ) -> list[InboundMessage]: ...

    # Epic 55 (C-3): read-only -- messages of ONE folder / label (the Demand Agent intake folder) received
    # after ``since_ref`` (ISO-8601 UTC of the newest message already seen), oldest first, at most ``limit``.
    # Uses the reply-capture read scopes; nothing outside that folder is listed.
    async def list_folder_messages(
        self, token_set: TokenSet, folder: str, since_ref: str | None, limit: int = 50
    ) -> list[ProviderMessage]: ...

    # Epic 72 (FR-371..FR-373): push channels and delta reads of Talent OS-linked events only.
    async def watch(
        self, token_set: TokenSet, channel_url: str, channel_id: str, token: str, ttl_seconds: int
    ) -> ChannelRef: ...

    async def renew_watch(self, token_set: TokenSet, ref: ChannelRef, ttl_seconds: int) -> ChannelRef: ...

    async def stop_watch(self, token_set: TokenSet, ref: ChannelRef) -> None: ...

    async def list_changes(
        self,
        token_set: TokenSet,
        sync_token: str | None,
        *,
        known_ids: frozenset[str],
        known_ical_uids: frozenset[str],
        window: tuple[datetime, datetime],
    ) -> tuple[list[EventChange], str]: ...

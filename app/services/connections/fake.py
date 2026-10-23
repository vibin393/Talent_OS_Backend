"""In-memory ``CalendarProvider`` (tests / dev, ``CALENDAR_PROVIDER_FAKE=true``). Never touches the network.

* ``exchange_code("<anything>")`` returns tokens for ``account_email`` (default ``<code>@fake.test`` unless set).
* Events live in ``self.events``; the meeting URL is deterministic: ``https://meet.fake/{event_id}``.
* ``busy`` maps email -> busy blocks returned by ``free_busy``.
* Failure injection: ``fail_refresh`` / ``fail_upsert`` / ``fail_send`` raise ``ProviderAuthError``;
  ``fail_revoke`` raises ``ProviderError``.
* Epic 72 (calendar sync): ``watch`` / ``renew_watch`` / ``stop_watch`` record channels;
  ``list_changes`` replays a change log (every write, including Talent OS's own upserts = echoes)
  filtered to the known ids / iCalUIDs; script helpers ``move_event``, ``respond``, ``delete_event``,
  ``add_foreign_event``, ``expire_channel``, ``invalidate_sync_token``; flags ``fail_watch`` /
  ``fail_delta``; ``notify_google`` / ``notify_microsoft`` build signed notifications for the ingress
  tests. ``writes`` counts provider writes (the loop test bounds it).
"""

import dataclasses
import itertools
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from app.core.mailer import EmailMessage
from app.services.connections.provider import (
    AttendeeResponse,
    BusyBlock,
    ChannelRef,
    EventChange,
    EventResult,
    EventSpec,
    InboundMessage,
    ProviderAuthError,
    ProviderError,
    ProviderMessage,
    SentRef,
    SyncTokenInvalid,
    TokenSet,
)

FAKE_SCOPES: tuple[str, ...] = ("calendar", "mail")


@dataclasses.dataclass
class FakeEvent:
    id: str
    spec: EventSpec
    meeting_url: str | None
    cancelled: bool = False
    updates: int = 0
    # Epic 72: calendar-side state (moves / responses / deletion) and the provider etag.
    owner: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    etag: str = ""
    tos_tag: str | None = None
    deleted: bool = False
    responses: dict[str, str] = dataclasses.field(default_factory=dict)
    title: str = ""


class FakeProvider:
    name = "fake"
    scopes = FAKE_SCOPES

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.events: dict[str, FakeEvent] = {}
        self.sent: list[EmailMessage] = []
        self.revoked: list[str] = []
        self.busy: dict[str, list[BusyBlock]] = {}
        self.account_email: str | None = None
        self.fail_refresh = False
        self.fail_upsert = False
        self.fail_send = False
        self.fail_revoke = False
        self.token_ttl = timedelta(hours=1)
        self.calls: list[str] = []
        self._ids = itertools.count(1)
        # Epic 33: captured threads (thread_ref -> messages), scopes requested by the last authorize_url.
        self.threads: dict[str, list[InboundMessage]] = {}
        self.pending_extra_scopes: tuple[str, ...] = ()
        self.fail_threads = False  # ProviderAuthError on list_thread_messages
        self.fail_threads_transient = False  # ProviderError on list_thread_messages
        # Epic 55: intake folders (folder name -> messages) for ``list_folder_messages``.
        self.folders: dict[str, list[ProviderMessage]] = {}
        self.fail_folders = False  # ProviderAuthError on list_folder_messages
        # Epic 72: push channels and the change log.
        self.channels: dict[str, dict[str, object]] = {}
        self.stopped_channels: list[str] = []
        self.change_log: list[tuple[int, str]] = []
        self._seq = 0
        self.token_epoch = 0
        self.fail_watch = False
        self.fail_delta = False
        self.fail_delta_auth = False
        self.renewable = False  # True: renew_watch extends in place (Graph-like); False: Google-like re-watch
        self.writes = 0
        self.delta_calls = 0

    def authorize_url(self, state: str, redirect_uri: str, extra_scopes: tuple[str, ...] = ()) -> str:
        self.pending_extra_scopes = tuple(extra_scopes)
        params = {"state": state, "redirect_uri": redirect_uri}
        if extra_scopes:
            params["scope"] = " ".join((*FAKE_SCOPES, *extra_scopes))
        return "https://auth.fake/authorize?" + urlencode(params)

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:  # ruff: ignore[unused-method-argument]
        self.calls.append("exchange_code")
        if code == "bad":
            raise ProviderAuthError("invalid_grant")
        n = next(self._ids)
        return TokenSet(
            access_token=f"fake-access-{n}",
            refresh_token=f"fake-refresh-{n}",
            expires_at=datetime.now(UTC) + self.token_ttl,
            scopes=(*FAKE_SCOPES, *self._take_extra()),
            account_email=self.account_email or f"{code}@fake.test",
        )

    def _take_extra(self) -> tuple[str, ...]:
        extra, self.pending_extra_scopes = self.pending_extra_scopes, ()
        return extra

    async def refresh(self, token_set: TokenSet) -> TokenSet:
        self.calls.append("refresh")
        if self.fail_refresh:
            raise ProviderAuthError("invalid_grant")
        n = next(self._ids)
        return dataclasses.replace(
            token_set, access_token=f"fake-access-{n}", expires_at=datetime.now(UTC) + self.token_ttl
        )

    async def revoke(self, token_set: TokenSet) -> None:
        self.calls.append("revoke")
        if self.fail_revoke:
            raise ProviderError("revoke failed")
        self.revoked.append(token_set.refresh_token or token_set.access_token)

    async def upsert_event(self, token_set: TokenSet, event: EventSpec) -> EventResult:
        self.calls.append("upsert_event")
        if self.fail_upsert:
            raise ProviderAuthError("unauthorized")
        self.writes += 1
        if event.provider_event_id and event.provider_event_id in self.events:
            existing = self.events[event.provider_event_id]
            existing.spec = event
            existing.updates += 1
            existing.cancelled = False
            existing.deleted = False
            existing.start, existing.end = event.start_utc, event.end_utc
            existing.tos_tag = event.tos_tag or existing.tos_tag
            if event.create_online_meeting and existing.meeting_url is None:
                existing.meeting_url = f"https://meet.fake/{existing.id}"
            self._touch(existing)
            return EventResult(existing.id, existing.meeting_url or event.meeting_url, existing.etag)
        event_id = f"evt{next(self._ids)}"
        url = f"https://meet.fake/{event_id}" if event.create_online_meeting else None
        row = FakeEvent(event_id, event, url, owner=(token_set.account_email or "").lower() or None)
        row.start, row.end, row.tos_tag = event.start_utc, event.end_utc, event.tos_tag
        self.events[event_id] = row
        self._touch(row)
        return EventResult(event_id, url or event.meeting_url, row.etag)

    async def cancel_event(self, token_set: TokenSet, provider_event_id: str) -> None:  # ruff: ignore[unused-method-argument]
        self.calls.append("cancel_event")
        if self.fail_upsert:
            raise ProviderAuthError("unauthorized")
        self.writes += 1
        if provider_event_id in self.events:
            self.events[provider_event_id].cancelled = True
            self._touch(self.events[provider_event_id])

    async def free_busy(
        self,
        token_set: TokenSet,  # ruff: ignore[unused-method-argument] - provider interface
        emails: list[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> dict[str, list[BusyBlock]]:
        self.calls.append("free_busy")
        out: dict[str, list[BusyBlock]] = {}
        for email in emails:
            blocks = [b for b in self.busy.get(email.lower(), []) if b.end > start_utc and b.start < end_utc]
            out[email.lower()] = blocks
        return out

    async def send_mail(self, token_set: TokenSet, message: EmailMessage) -> SentRef:  # ruff: ignore[unused-method-argument]
        self.calls.append("send_mail")
        if self.fail_send:
            raise ProviderAuthError("unauthorized")
        self.sent.append(message)
        n = next(self._ids)
        self.threads.setdefault(f"fake-thread-{n}", [])
        return SentRef(f"fake-msg-{n}", f"fake-thread-{n}", f"<fake-msg-{n}@fake.test>")

    # ---------------------------------------------------------------- Epic 33: reply capture
    def add_reply(
        self,
        thread_ref: str,
        from_address: str,
        body_text: str = "Thanks, sounds good.",
        *,
        subject: str = "Re: hello",
        body_html: str | None = None,
        attachment_names: tuple[str, ...] = (),
        sent_at: datetime | None = None,
    ) -> InboundMessage:
        """Append a message to a captured thread (tests); returns it."""
        n = next(self._ids)
        message = InboundMessage(
            provider_message_id=f"fake-in-{n}",
            rfc_message_id=f"<fake-in-{n}@fake.test>",
            in_reply_to=None,
            from_address=from_address,
            to=(),
            cc=(),
            subject=subject,
            sent_at=sent_at or datetime.now(UTC),
            body_text=body_text,
            body_html=body_html,
            attachment_names=attachment_names,
        )
        self.threads.setdefault(thread_ref, []).append(message)
        return message

    async def list_thread_messages(
        self, token_set: TokenSet, thread_ref: str, since_ref: str | None  # ruff: ignore[unused-method-argument]
    ) -> list[InboundMessage]:
        self.calls.append(f"list_thread_messages:{thread_ref}")
        if self.fail_threads:
            raise ProviderAuthError("unauthorized")
        if self.fail_threads_transient:
            raise ProviderError("fake 503")
        return list(self.threads.get(thread_ref, []))

    # ---------------------------------------------------------------- Epic 55: intake folder
    def add_folder_message(
        self, folder: str, raw: bytes, *, received_at: datetime | None = None
    ) -> ProviderMessage:
        """Append a raw RFC 822 message to an intake folder (tests); returns it."""
        n = next(self._ids)
        message = ProviderMessage(
            provider_message_id=f"fake-folder-{n}",
            rfc_message_id=None,
            received_at=received_at or datetime.now(UTC),
            raw=raw,
        )
        self.folders.setdefault(folder, []).append(message)
        return message

    async def list_folder_messages(
        self,
        token_set: TokenSet,  # ruff: ignore[unused-method-argument]
        folder: str,
        since_ref: str | None,
        limit: int = 50,
    ) -> list[ProviderMessage]:
        self.calls.append(f"list_folder_messages:{folder}")
        if self.fail_folders:
            raise ProviderAuthError("unauthorized")
        items = sorted(self.folders.get(folder, []), key=lambda m: m.received_at)
        if since_ref:
            items = [m for m in items if m.received_at.isoformat() > since_ref]
        return items[:limit]

    # ---------------------------------------------------------------- Epic 72: calendar sync
    def _touch(self, event: FakeEvent) -> None:
        self._seq += 1
        event.etag = f'"etag-{event.id}-{self._seq}"'
        self.change_log.append((self._seq, event.id))

    def move_event(
        self, event_id: str, new_start: datetime, new_end: datetime, by_email: str | None = None  # ruff: ignore[unused-method-argument]
    ) -> None:
        """Simulate a drag in the calendar (the private extended properties survive, as in Google)."""
        event = self.events[event_id]
        event.start, event.end = new_start, new_end
        self._touch(event)

    def respond(self, event_id: str, email: str, response: str) -> None:
        event = self.events[event_id]
        event.responses[email.lower()] = response
        self._touch(event)

    def delete_event(self, event_id: str) -> None:
        event = self.events[event_id]
        event.deleted = True
        self._touch(event)

    def add_foreign_event(self, owner: str, title: str, start: datetime, end: datetime) -> str:
        """Add a non-Talent OS event to ``owner``'s calendar (must never leave ``list_changes``)."""
        event_id = f"foreign{next(self._ids)}"
        spec = EventSpec(f"foreign-{event_id}@x", title, title, start, end, "UTC", ())
        row = FakeEvent(event_id, spec, None, owner=owner.lower(), start=start, end=end, title=title)
        self.events[event_id] = row
        self._touch(row)
        return event_id

    def invalidate_sync_token(self) -> None:
        self.token_epoch += 1

    def expire_channel(self, channel_id: str) -> None:
        if channel_id in self.channels:
            self.channels[channel_id]["expired"] = True

    def notify_google(
        self, channel_id: str, *, token: str | None = None, state: str = "exists"
    ) -> dict[str, str]:
        ch = self.channels[channel_id]
        return {
            "X-Goog-Channel-ID": channel_id,
            "X-Goog-Channel-Token": token if token is not None else str(ch["token"]),
            "X-Goog-Resource-ID": str(ch["resource_id"]),
            "X-Goog-Resource-State": state,
            "X-Goog-Message-Number": str(self._seq),
        }

    def notify_microsoft(self, channel_id: str, *, client_state: str | None = None) -> dict[str, object]:
        ch = self.channels[channel_id]
        return {
            "value": [
                {
                    "subscriptionId": str(ch["resource_id"]),
                    "clientState": client_state if client_state is not None else str(ch["token"]),
                    "changeType": "updated",
                    "resource": "me/events/x",
                    "resourceData": {"id": "x"},
                }
            ]
        }

    async def watch(
        self, token_set: TokenSet, channel_url: str, channel_id: str, token: str, ttl_seconds: int
    ) -> ChannelRef:
        self.calls.append("watch")
        if self.fail_watch:
            raise ProviderError("fake watch failed")
        resource_id = f"res-{channel_id[:8]}-{next(self._ids)}"
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        self.channels[channel_id] = {
            "url": channel_url, "token": token, "resource_id": resource_id, "expires_at": expires,
            "account": (token_set.account_email or "").lower(),
        }  # fmt: skip
        return ChannelRef(channel_id, resource_id, expires)

    async def renew_watch(self, token_set: TokenSet, ref: ChannelRef, ttl_seconds: int) -> ChannelRef:  # ruff: ignore[unused-method-argument]
        self.calls.append("renew_watch")
        if self.fail_watch or ref.channel_id not in self.channels:
            raise ProviderError("fake renew failed")
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        self.channels[ref.channel_id]["expires_at"] = expires
        return ChannelRef(ref.channel_id, ref.resource_id, expires)

    async def stop_watch(self, token_set: TokenSet, ref: ChannelRef) -> None:  # ruff: ignore[unused-method-argument]
        self.calls.append("stop_watch")
        self.channels.pop(ref.channel_id, None)
        self.stopped_channels.append(ref.channel_id)

    def _change(self, e: FakeEvent, account: str) -> EventChange | None:
        is_owner = e.owner == account
        attendee_emails = {a.email.lower() for a in e.spec.attendees}
        if not is_owner and account not in attendee_emails:
            return None
        status = "deleted" if e.deleted else ("cancelled" if e.cancelled else "confirmed")
        if is_owner:
            attendees = tuple(
                AttendeeResponse(a.email.lower(), e.responses.get(a.email.lower(), "needs_action"))  # type: ignore[arg-type]
                for a in e.spec.attendees
            )
            return EventChange(
                e.id, e.spec.ical_uid, status, e.start, e.end, e.etag, e.tos_tag, attendees, True  # type: ignore[arg-type]
            )
        # an attendee's own copy: its own id, the same iCalUID, only their own response
        own = AttendeeResponse(account, e.responses.get(account, "needs_action"), is_self=True)  # type: ignore[arg-type]
        copy_id = f"{e.id}~{account}"
        return EventChange(
            copy_id, e.spec.ical_uid, status, e.start, e.end, e.etag, None, (own,), False  # type: ignore[arg-type]
        )

    async def list_changes(
        self,
        token_set: TokenSet,
        sync_token: str | None,
        *,
        known_ids: frozenset[str],
        known_ical_uids: frozenset[str],
        window: tuple[datetime, datetime],
    ) -> tuple[list[EventChange], str]:
        self.calls.append("list_changes")
        self.delta_calls += 1
        if self.fail_delta_auth:
            raise ProviderAuthError("unauthorized")
        if self.fail_delta:
            raise ProviderError("fake delta failed")
        account = (token_set.account_email or "").lower()
        since = 0
        if sync_token is not None:
            epoch, _, seq = sync_token.partition(":")
            if epoch != str(self.token_epoch) or not seq.isdigit():
                raise SyncTokenInvalid("fake sync token invalid")
            since = int(seq)
        ids = list(dict.fromkeys(eid for seq, eid in self.change_log if seq > since))
        out: list[EventChange] = []
        for eid in ids:
            e = self.events.get(eid)
            if e is None:
                continue
            if sync_token is None and e.start is not None and not (window[0] <= e.start <= window[1]):
                continue
            change = self._change(e, account)
            # privacy: everything not linked to Talent OS is dropped HERE (never returned)
            if change is None or not (
                change.provider_event_id in known_ids or (change.ical_uid or "") in known_ical_uids
            ):
                continue
            out.append(change)
        return out, f"{self.token_epoch}:{self._seq}"


_instance = FakeProvider()


def instance() -> FakeProvider:
    """Process-wide fake (tests reach it to inspect events or inject failures)."""
    return _instance

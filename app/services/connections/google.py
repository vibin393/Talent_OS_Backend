"""Google Workspace ``CalendarProvider``: OAuth 2.0, Calendar v3 (events with Meet links, freeBusy) and Gmail send.

Minimal scopes (``GOOGLE_SCOPES``). ``sendUpdates=all`` lets Google email the invitations. Never log tokens.
"""

import base64
import email.message
import email.policy
import email.utils
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.core.mailer import EmailMessage
from app.core.settings import get_settings
from app.services.connections.provider import (
    GOOGLE_SCOPES,
    HTTP_TIMEOUT_SECONDS,
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

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR = "https://www.googleapis.com/calendar/v3"
GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
# Epic 33: the ONLY read endpoint used for reply capture (one thread by id; never list / search).
GMAIL_THREAD = "https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}"
# Epic 55 (C-3): the Demand Agent intake label only -- labels (resolve the name), that label's messages.
GMAIL_LABELS = "https://gmail.googleapis.com/gmail/v1/users/me/labels"
GMAIL_MESSAGES = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _check(resp: httpx.Response, what: str) -> dict[str, Any]:
    if resp.status_code in (401, 403) or (resp.status_code == 400 and "invalid_grant" in resp.text):
        raise ProviderAuthError(f"google {what}: {resp.status_code}")
    if resp.status_code >= 400:
        raise ProviderError(f"google {what}: {resp.status_code}")
    if not resp.content:
        return {}
    try:
        return dict(resp.json())
    except ValueError as exc:
        raise ProviderError(f"google {what}: invalid response") from exc


class GoogleProvider:
    name = "google"
    scopes = GOOGLE_SCOPES

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)

    def authorize_url(self, state: str, redirect_uri: str, extra_scopes: tuple[str, ...] = ()) -> str:
        s = get_settings()
        params = {
            "client_id": s.google_oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(dict.fromkeys((*self.scopes, *extra_scopes))),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        s = get_settings()
        payload = {
            "client_id": s.google_oauth_client_id,
            "client_secret": s.google_oauth_client_secret,
            **data,
        }
        async with self._client() as client:
            return _check(await client.post(TOKEN_URL, data=payload), "token")

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        body = await self._token_request(
            {"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
        )
        access = str(body.get("access_token") or "")
        if not access:
            raise ProviderError("google token: no access token")
        account = None
        async with self._client() as client:
            info = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
            if info.status_code == 200:
                account = info.json().get("email")
        return TokenSet(
            access_token=access,
            refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in") or 3600)),
            scopes=tuple(str(body.get("scope") or " ".join(self.scopes)).split()),
            account_email=account,
        )

    async def refresh(self, token_set: TokenSet) -> TokenSet:
        if not token_set.refresh_token:
            raise ProviderAuthError("google refresh: no refresh token")
        body = await self._token_request(
            {"refresh_token": token_set.refresh_token, "grant_type": "refresh_token"}
        )
        return TokenSet(
            access_token=str(body.get("access_token") or ""),
            refresh_token=body.get("refresh_token") or token_set.refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in") or 3600)),
            scopes=token_set.scopes,
            account_email=token_set.account_email,
        )

    async def revoke(self, token_set: TokenSet) -> None:
        token = token_set.refresh_token or token_set.access_token
        async with self._client() as client:
            resp = await client.post(REVOKE_URL, data={"token": token})
        if resp.status_code >= 500:  # 400 = token already invalid: nothing left to revoke
            raise ProviderError(f"google revoke: {resp.status_code}")

    @staticmethod
    def _auth(token_set: TokenSet) -> dict[str, str]:
        return {"Authorization": f"Bearer {token_set.access_token}"}

    async def upsert_event(self, token_set: TokenSet, event: EventSpec) -> EventResult:
        body: dict[str, Any] = {
            "summary": event.summary,
            "description": event.description,
            "start": {"dateTime": _iso(event.start_utc), "timeZone": event.timezone},
            "end": {"dateTime": _iso(event.end_utc), "timeZone": event.timezone},
            "attendees": [{"email": a.email, "displayName": a.name or a.email} for a in event.attendees],
            "sequence": event.sequence,
        }
        if event.location:
            body["location"] = event.location
        if event.tos_tag:  # Epic 72: the version tag (echo detection); P1 callers never set it
            body["extendedProperties"] = {
                "private": {"tos_tag": event.tos_tag, "tos_interview": event.tos_tag.split(":")[1]}
            }
        if event.create_online_meeting:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": f"{event.ical_uid}-{event.sequence}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        params = {"sendUpdates": "all", "conferenceDataVersion": "1"}
        async with self._client() as client:
            if event.provider_event_id:
                resp = await client.patch(
                    f"{CALENDAR}/calendars/primary/events/{event.provider_event_id}",
                    params=params,
                    json=body,
                    headers=self._auth(token_set),
                )
            else:
                body["iCalUID"] = event.ical_uid
                resp = await client.post(
                    f"{CALENDAR}/calendars/primary/events",
                    params=params,
                    json=body,
                    headers=self._auth(token_set),
                )
        data = _check(resp, "event")
        meet = data.get("hangoutLink")
        for entry in (data.get("conferenceData") or {}).get("entryPoints") or []:
            if entry.get("entryPointType") == "video":
                meet = entry.get("uri") or meet
        return EventResult(str(data.get("id")), meet or event.meeting_url, data.get("etag"))

    async def cancel_event(self, token_set: TokenSet, provider_event_id: str) -> None:
        async with self._client() as client:
            resp = await client.delete(
                f"{CALENDAR}/calendars/primary/events/{provider_event_id}",
                params={"sendUpdates": "all"},
                headers=self._auth(token_set),
            )
        if resp.status_code in (404, 410):
            return
        _check(resp, "cancel")

    async def free_busy(
        self, token_set: TokenSet, emails: list[str], start_utc: datetime, end_utc: datetime
    ) -> dict[str, list[BusyBlock]]:
        body = {"timeMin": _iso(start_utc), "timeMax": _iso(end_utc), "items": [{"id": e} for e in emails]}
        async with self._client() as client:
            data = _check(
                await client.post(f"{CALENDAR}/freeBusy", json=body, headers=self._auth(token_set)),
                "freebusy",
            )
        out: dict[str, list[BusyBlock]] = {}
        for email_addr, cal in (data.get("calendars") or {}).items():
            out[email_addr.lower()] = [
                BusyBlock(_parse(b["start"]), _parse(b["end"])) for b in cal.get("busy") or []
            ]
        return out

    async def send_mail(self, token_set: TokenSet, message: EmailMessage) -> SentRef | None:
        raw = build_rfc822(message, token_set.account_email)
        async with self._client() as client:
            data = _check(
                await client.post(
                    GMAIL_SEND,
                    json={"raw": base64.urlsafe_b64encode(raw).decode("ascii")},
                    headers=self._auth(token_set),
                ),
                "send",
            )
        if not data.get("id"):
            return None
        return SentRef(str(data["id"]), str(data.get("threadId") or "") or None, None)

    async def list_thread_messages(
        self, token_set: TokenSet, thread_ref: str, since_ref: str | None
    ) -> list[InboundMessage]:
        """Epic 33: ``users.threads.get(thread_ref, format=full)`` -- nothing else in the mailbox is read."""
        async with self._client() as client:
            data = _check(
                await client.get(
                    GMAIL_THREAD.format(thread_id=quote(thread_ref, safe="")),
                    params={"format": "full"},
                    headers=self._auth(token_set),
                ),
                "thread",
            )
        out = [parse_gmail_message(m) for m in data.get("messages") or []]
        if since_ref:
            ids = [m.provider_message_id for m in out]
            if since_ref in ids:
                out = out[ids.index(since_ref) + 1 :]
        return out

    async def list_folder_messages(
        self, token_set: TokenSet, folder: str, since_ref: str | None, limit: int = 50
    ) -> list[ProviderMessage]:
        """Epic 55: messages with the label named ``folder`` (``format=raw``), newer than ``since_ref``."""
        since = _parse(since_ref) if since_ref else None
        async with self._client() as client:
            labels = _check(await client.get(GMAIL_LABELS, headers=self._auth(token_set)), "labels")
            label_id = next(
                (
                    lb.get("id")
                    for lb in labels.get("labels") or []
                    if str(lb.get("name", "")).lower() == folder.lower()
                ),
                None,
            )
            if not label_id:
                return []
            params: dict[str, Any] = {"labelIds": label_id, "maxResults": str(max(1, min(limit, 100)))}
            if since is not None:
                params["q"] = f"after:{int(since.timestamp())}"
            listing = _check(
                await client.get(GMAIL_MESSAGES, params=params, headers=self._auth(token_set)), "messages"
            )
            out: list[ProviderMessage] = []
            for ref in listing.get("messages") or []:
                msg = _check(
                    await client.get(
                        f"{GMAIL_MESSAGES}/{quote(str(ref.get('id')), safe='')}",
                        params={"format": "raw"},
                        headers=self._auth(token_set),
                    ),
                    "message",
                )
                internal = msg.get("internalDate")
                received = (
                    datetime.fromtimestamp(int(internal) / 1000, UTC) if internal else datetime.now(UTC)
                )
                if since is not None and received <= since:
                    continue
                raw = str(msg.get("raw") or "")
                data = base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode("ascii")) if raw else b""
                out.append(ProviderMessage(str(msg.get("id")), None, received, data))
        out.sort(key=lambda m: m.received_at)
        return out[:limit]

    # ---------------------------------------------------------------- Epic 72: push channels + delta sync
    async def watch(
        self, token_set: TokenSet, channel_url: str, channel_id: str, token: str, ttl_seconds: int
    ) -> ChannelRef:
        body = {
            "id": channel_id,
            "type": "web_hook",
            "address": channel_url,
            "token": token,
            "params": {"ttl": str(ttl_seconds)},
        }
        async with self._client() as client:
            data = _check(
                await client.post(
                    f"{CALENDAR}/calendars/primary/events/watch", json=body, headers=self._auth(token_set)
                ),
                "watch",
            )
        expiration = data.get("expiration")
        expires = datetime.fromtimestamp(int(expiration) / 1000, UTC) if expiration else None
        return ChannelRef(channel_id, data.get("resourceId"), expires)

    async def renew_watch(
        self,
        token_set: TokenSet,  # noqa: ARG002
        ref: ChannelRef,  # noqa: ARG002
        ttl_seconds: int,  # noqa: ARG002
    ) -> ChannelRef:
        raise ProviderError("google channels cannot be extended; watch a new channel")

    async def stop_watch(self, token_set: TokenSet, ref: ChannelRef) -> None:
        async with self._client() as client:
            resp = await client.post(
                f"{CALENDAR}/channels/stop",
                json={"id": ref.channel_id, "resourceId": ref.resource_id},
                headers=self._auth(token_set),
            )
        if resp.status_code == 404:
            return
        _check(resp, "stop_watch")

    async def list_changes(
        self,
        token_set: TokenSet,
        sync_token: str | None,
        *,
        known_ids: frozenset[str],
        known_ical_uids: frozenset[str],
        window: tuple[datetime, datetime],
    ) -> tuple[list[EventChange], str]:
        """Delta of the primary calendar; ONLY items whose id / iCalUID are known leave this method (FR-371).
        Summary, description and location are never requested (``fields``)."""
        params: dict[str, str] = {
            "showDeleted": "true",
            "singleEvents": "true",
            "maxResults": "250",
            "fields": LIST_FIELDS,
        }
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["timeMin"] = _iso(window[0])
            params["timeMax"] = _iso(window[1])
        out: list[EventChange] = []
        next_sync = ""
        async with self._client() as client:
            page: str | None = None
            for _ in range(50):
                query = {**params, **({"pageToken": page} if page else {})}
                resp = await client.get(
                    f"{CALENDAR}/calendars/primary/events", params=query, headers=self._auth(token_set)
                )
                if resp.status_code == 410:
                    raise SyncTokenInvalid("google events: 410")
                data = _check(resp, "events")
                for item in data.get("items") or []:
                    change = _google_change(item, known_ids, known_ical_uids)
                    if change is not None:
                        out.append(change)
                page = data.get("nextPageToken")
                if not page:
                    next_sync = str(data.get("nextSyncToken") or sync_token or "")
                    break
        return out, next_sync


def _b64(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _addresses(value: str | None) -> tuple[str, ...]:
    return tuple(addr.lower() for _, addr in email.utils.getaddresses([value or ""]) if addr)


def parse_gmail_message(msg: dict[str, Any]) -> InboundMessage:
    """One Gmail ``format=full`` message -> ``InboundMessage`` (text / html parts, attachment names only)."""
    payload = msg.get("payload") or {}
    headers = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers") or []}
    text_parts: list[str] = []
    html_parts: list[str] = []
    names: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        if part.get("filename"):
            names.append(str(part["filename"])[:255])
        elif mime == "text/plain" and body.get("data"):
            text_parts.append(_b64(body.get("data")))
        elif mime == "text/html" and body.get("data"):
            html_parts.append(_b64(body.get("data")))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    internal = msg.get("internalDate")
    sent_at = datetime.fromtimestamp(int(internal) / 1000, UTC) if internal else datetime.now(UTC)
    from_list = _addresses(headers.get("from"))
    return InboundMessage(
        provider_message_id=str(msg.get("id")),
        rfc_message_id=headers.get("message-id") or None,
        in_reply_to=headers.get("in-reply-to") or None,
        from_address=from_list[0] if from_list else "",
        to=_addresses(headers.get("to")),
        cc=_addresses(headers.get("cc")),
        subject=headers.get("subject", ""),
        sent_at=sent_at,
        body_text="\n".join(text_parts),
        body_html="\n".join(html_parts) or None,
        attachment_names=tuple(names),
    )


def build_rfc822(message: EmailMessage, sender: str | None) -> bytes:
    """RFC 822 bytes of ``message`` (text + optional html alternative + attachments)."""
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    if sender:
        msg["From"] = sender
    msg["To"] = message.to
    if message.cc:
        msg["Cc"] = ", ".join(message.cc)
    if message.reply_to:
        msg["Reply-To"] = message.reply_to
    msg["Subject"] = message.subject
    for key, value in message.headers.items():
        msg[key] = value
    msg.set_content(message.text)
    if message.html:
        msg.add_alternative(message.html, subtype="html")
    for att in message.attachments:
        maintype, _, subtype = att.content_type.partition(";")[0].strip().partition("/")
        msg.add_attachment(
            att.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )
    return msg.as_bytes()


# Epic 72: the minimum fields of an events page (never summary / description / location).
LIST_FIELDS = (
    "nextPageToken,nextSyncToken,items(id,iCalUID,status,start,end,etag,organizer/self,"
    "attendees(email,responseStatus,self),extendedProperties/private)"
)
_GOOGLE_RESPONSES = {
    "needsAction": "needs_action",
    "accepted": "accepted",
    "tentative": "tentative",
    "declined": "declined",
}


def _google_time(value: dict[str, Any] | None) -> datetime | None:
    if not value or not value.get("dateTime"):
        return None
    return _parse(str(value["dateTime"])).astimezone(UTC)


def _google_change(
    item: dict[str, Any], known_ids: frozenset[str], known_ical_uids: frozenset[str]
) -> EventChange | None:
    """Map one events item; None (dropped, never stored or logged) unless it is a Talent OS-linked event."""
    event_id = str(item.get("id") or "")
    ical = item.get("iCalUID")
    if event_id not in known_ids and (ical or "") not in known_ical_uids:
        return None
    organizer_self = bool((item.get("organizer") or {}).get("self", True))
    cancelled = item.get("status") == "cancelled"
    status = "deleted" if cancelled else ("tentative" if item.get("status") == "tentative" else "confirmed")
    attendees = tuple(
        AttendeeResponse(
            str(a.get("email") or "").lower(),
            _GOOGLE_RESPONSES.get(str(a.get("responseStatus") or ""), "needs_action"),  # type: ignore[arg-type]
            bool(a.get("self")),
        )
        for a in item.get("attendees") or []
        if a.get("email")
    )
    private = (item.get("extendedProperties") or {}).get("private") or {}
    return EventChange(
        provider_event_id=event_id,
        ical_uid=ical,
        status=status,  # type: ignore[arg-type]
        start_utc=_google_time(item.get("start")),
        end_utc=_google_time(item.get("end")),
        etag=item.get("etag"),
        tos_tag=private.get("tos_tag"),
        attendees=attendees,
        organizer_is_self=organizer_self,
    )

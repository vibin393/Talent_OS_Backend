"""Microsoft 365 ``CalendarProvider``: OAuth 2.0 (v2 endpoint), Graph events with Teams links, getSchedule
free/busy and ``/me/sendMail``. Minimal scopes (``MICROSOFT_SCOPES``). Never log tokens.

Graph has no token revocation endpoint for delegated refresh tokens: ``revoke`` is a no-op locally (the
stored tokens are wiped by the service); users can also remove the app from their account.
"""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.mailer import EmailMessage
from app.core.settings import get_settings
from app.services.connections.provider import (
    HTTP_TIMEOUT_SECONDS,
    MICROSOFT_REPLY_SCOPES,
    MICROSOFT_SCOPES,
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

GRAPH = "https://graph.microsoft.com/v1.0"
# Epic 72: the fixed property-set GUID of the Talent OS version tag (single-value extended property).
TOS_PROP_SET = "0b7f1f5e-7f2a-4a39-9d37-2f8a6d3c7a11"
TOS_TAG_PROP = f"String {{{TOS_PROP_SET}}} Name tosTag"
MAX_SUBSCRIPTION_MINUTES = 10080


def _login(path: str) -> str:
    return (
        f"https://login.microsoftonline.com/{get_settings().ms_oauth_tenant or 'common'}/oauth2/v2.0/{path}"
    )


def _check(resp: httpx.Response, what: str) -> dict[str, Any]:
    if resp.status_code == 401 or (resp.status_code == 400 and "invalid_grant" in resp.text):
        raise ProviderAuthError(f"microsoft {what}: {resp.status_code}")
    if resp.status_code >= 400:
        raise ProviderError(f"microsoft {what}: {resp.status_code}")
    if not resp.content:
        return {}
    try:
        return dict(resp.json())
    except ValueError as exc:
        raise ProviderError(f"microsoft {what}: invalid response") from exc


def _graph_time(dt: datetime) -> dict[str, str]:
    return {
        "dateTime": dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "timeZone": "UTC",
    }


def _parse_graph(value: dict[str, Any]) -> datetime:
    raw = str(value.get("dateTime", "")).split(".")[0]
    return datetime.fromisoformat(raw).replace(tzinfo=UTC)


class MicrosoftProvider:
    name = "microsoft"
    scopes = MICROSOFT_SCOPES

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)

    def authorize_url(self, state: str, redirect_uri: str, extra_scopes: tuple[str, ...] = ()) -> str:
        params = {
            "client_id": get_settings().ms_oauth_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": " ".join(dict.fromkeys((*self.scopes, *extra_scopes))),
            "state": state,
        }
        return f"{_login('authorize')}?{urlencode(params)}"

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        s = get_settings()
        payload = {
            "client_id": s.ms_oauth_client_id,
            "client_secret": s.ms_oauth_client_secret,
            "scope": " ".join(self.scopes),
            **data,
        }
        async with self._client() as client:
            return _check(await client.post(_login("token"), data=payload), "token")

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        body = await self._token_request(
            {"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
        )
        access = str(body.get("access_token") or "")
        if not access:
            raise ProviderError("microsoft token: no access token")
        account = None
        async with self._client() as client:
            me = await client.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {access}"})
            if me.status_code == 200:
                data = me.json()
                account = data.get("mail") or data.get("userPrincipalName")
        return TokenSet(
            access_token=access,
            refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in") or 3600)),
            scopes=tuple(str(body.get("scope") or " ".join(self.scopes)).split()),
            account_email=account,
        )

    async def refresh(self, token_set: TokenSet) -> TokenSet:
        if not token_set.refresh_token:
            raise ProviderAuthError("microsoft refresh: no refresh token")
        request = {"refresh_token": token_set.refresh_token, "grant_type": "refresh_token"}
        if set(MICROSOFT_REPLY_SCOPES) <= set(token_set.scopes or ()):
            # Epic 33: keep the granted reply-capture scopes on refresh.
            request["scope"] = " ".join(dict.fromkeys((*self.scopes, *MICROSOFT_REPLY_SCOPES)))
        body = await self._token_request(request)
        return TokenSet(
            access_token=str(body.get("access_token") or ""),
            refresh_token=body.get("refresh_token") or token_set.refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in") or 3600)),
            scopes=token_set.scopes,
            account_email=token_set.account_email,
        )

    async def revoke(self, token_set: TokenSet) -> None:  # ruff: ignore[unused-method-argument] - see module docstring
        return None

    @staticmethod
    def _auth(token_set: TokenSet) -> dict[str, str]:
        return {"Authorization": f"Bearer {token_set.access_token}"}

    async def upsert_event(self, token_set: TokenSet, event: EventSpec) -> EventResult:
        body: dict[str, Any] = {
            "subject": event.summary,
            "body": {"contentType": "text", "content": event.description},
            "start": _graph_time(event.start_utc),
            "end": _graph_time(event.end_utc),
            "attendees": [
                {"emailAddress": {"address": a.email, "name": a.name or a.email}, "type": "required"}
                for a in event.attendees
            ],
        }
        if event.location:
            body["location"] = {"displayName": event.location}
        if event.tos_tag:  # Epic 72: the version tag (echo detection); P1 callers never set it
            body["singleValueExtendedProperties"] = [{"id": TOS_TAG_PROP, "value": event.tos_tag}]
        if event.create_online_meeting:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        async with self._client() as client:
            if event.provider_event_id:
                resp = await client.patch(
                    f"{GRAPH}/me/events/{event.provider_event_id}", json=body, headers=self._auth(token_set)
                )
            else:
                body["transactionId"] = event.ical_uid
                resp = await client.post(f"{GRAPH}/me/events", json=body, headers=self._auth(token_set))
        data = _check(resp, "event")
        url = ((data.get("onlineMeeting") or {}).get("joinUrl")) or event.meeting_url
        return EventResult(str(data.get("id")), url, data.get("changeKey") or data.get("@odata.etag"))

    async def cancel_event(self, token_set: TokenSet, provider_event_id: str) -> None:
        async with self._client() as client:
            resp = await client.post(
                f"{GRAPH}/me/events/{provider_event_id}/cancel",
                json={"comment": "This interview was cancelled."},
                headers=self._auth(token_set),
            )
        if resp.status_code == 404:
            return
        _check(resp, "cancel")

    async def free_busy(
        self, token_set: TokenSet, emails: list[str], start_utc: datetime, end_utc: datetime
    ) -> dict[str, list[BusyBlock]]:
        body = {
            "schedules": emails,
            "startTime": _graph_time(start_utc),
            "endTime": _graph_time(end_utc),
            "availabilityViewInterval": 30,
        }
        async with self._client() as client:
            data = _check(
                await client.post(
                    f"{GRAPH}/me/calendar/getSchedule", json=body, headers=self._auth(token_set)
                ),
                "freebusy",
            )
        out: dict[str, list[BusyBlock]] = {}
        for item in data.get("value") or []:
            blocks = [
                BusyBlock(_parse_graph(i["start"]), _parse_graph(i["end"]))
                for i in item.get("scheduleItems") or []
                if i.get("status") in ("busy", "tentative", "oof", "workingElsewhere")
            ]
            out[str(item.get("scheduleId", "")).lower()] = blocks
        return out

    async def send_mail(self, token_set: TokenSet, message: EmailMessage) -> SentRef:
        payload: dict[str, Any] = {
            "message": {
                "subject": message.subject,
                "body": {
                    "contentType": "HTML" if message.html else "Text",
                    "content": message.html or message.text,
                },
                "toRecipients": [{"emailAddress": {"address": message.to}}],
                "ccRecipients": [{"emailAddress": {"address": c}} for c in message.cc],
                "attachments": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": a.filename,
                        "contentType": a.content_type,
                        "contentBytes": base64.b64encode(a.data).decode("ascii"),
                    }
                    for a in message.attachments
                ],
            },
            "saveToSentItems": True,
        }
        if message.reply_to:
            payload["message"]["replyTo"] = [{"emailAddress": {"address": message.reply_to}}]
        # Epic 33: draft + send (instead of ``/me/sendMail``) so the conversation id is known for reply capture.
        async with self._client() as client:
            draft = _check(
                await client.post(
                    f"{GRAPH}/me/messages", json=payload["message"], headers=self._auth(token_set)
                ),
                "draft",
            )
            draft_id = str(draft.get("id") or "")
            if not draft_id:
                raise ProviderError("microsoft draft: no id")
            _check(
                await client.post(f"{GRAPH}/me/messages/{draft_id}/send", headers=self._auth(token_set)),
                "send",
            )
        return SentRef(draft_id, draft.get("conversationId") or None, draft.get("internetMessageId") or None)

    async def list_thread_messages(
        self, token_set: TokenSet, thread_ref: str, since_ref: str | None
    ) -> list[InboundMessage]:
        """Epic 33: ``GET /me/messages?$filter=conversationId eq '<ref>'`` (one conversation only); attachment
        names come from that message's own ``/attachments`` (names only, never bytes)."""
        safe_ref = thread_ref.replace("'", "''")
        params = {
            "$filter": f"conversationId eq '{safe_ref}'",
            "$select": "id,internetMessageId,from,toRecipients,ccRecipients,subject,receivedDateTime,body,hasAttachments",
            "$top": "50",
        }
        out: list[InboundMessage] = []
        async with self._client() as client:
            data = _check(
                await client.get(f"{GRAPH}/me/messages", params=params, headers=self._auth(token_set)),
                "thread",
            )
            for item in data.get("value") or []:
                names: tuple[str, ...] = ()
                if item.get("hasAttachments"):
                    att = _check(
                        await client.get(
                            f"{GRAPH}/me/messages/{item.get('id')}/attachments",
                            params={"$select": "name"},
                            headers=self._auth(token_set),
                        ),
                        "attachments",
                    )
                    names = tuple(
                        str(a.get("name") or "")[:255] for a in att.get("value") or [] if a.get("name")
                    )
                out.append(parse_graph_message(item, names))
        out.sort(key=lambda m: m.sent_at)
        if since_ref:
            ids = [m.provider_message_id for m in out]
            if since_ref in ids:
                out = out[ids.index(since_ref) + 1 :]
        return out

    async def list_folder_messages(
        self, token_set: TokenSet, folder: str, since_ref: str | None, limit: int = 50
    ) -> list[ProviderMessage]:
        """Epic 55: messages of the mail folder ``folder`` (``/$value`` MIME), newer than ``since_ref``."""
        safe_name = folder.replace("'", "''")
        out: list[ProviderMessage] = []
        async with self._client() as client:
            folders = _check(
                await client.get(
                    f"{GRAPH}/me/mailFolders",
                    params={"$filter": f"displayName eq '{safe_name}'", "$select": "id,displayName"},
                    headers=self._auth(token_set),
                ),
                "folders",
            )
            found = next(iter(folders.get("value") or []), None)
            if not found:
                return []
            params: dict[str, str] = {
                "$select": "id,internetMessageId,receivedDateTime",
                "$orderby": "receivedDateTime asc",
                "$top": str(max(1, min(limit, 100))),
            }
            if since_ref:
                params["$filter"] = f"receivedDateTime gt {since_ref.replace('+00:00', 'Z')}"
            listing = _check(
                await client.get(
                    f"{GRAPH}/me/mailFolders/{found.get('id')}/messages",
                    params=params,
                    headers=self._auth(token_set),
                ),
                "folder messages",
            )
            for item in listing.get("value") or []:
                resp = await client.get(
                    f"{GRAPH}/me/messages/{item.get('id')}/$value", headers=self._auth(token_set)
                )
                if resp.status_code in (401, 403):
                    raise ProviderAuthError(f"microsoft message: {resp.status_code}")
                if resp.status_code >= 400:
                    raise ProviderError(f"microsoft message: {resp.status_code}")
                received_raw = str(item.get("receivedDateTime") or "")
                received = (
                    datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
                    if received_raw
                    else datetime.now(UTC)
                )
                out.append(
                    ProviderMessage(
                        str(item.get("id")), item.get("internetMessageId") or None, received, resp.content
                    )
                )
        out.sort(key=lambda m: m.received_at)
        return out[:limit]

    # ---------------------------------------------------------------- Epic 72: subscriptions + delta sync
    async def watch(
        self, token_set: TokenSet, channel_url: str, channel_id: str, token: str, ttl_seconds: int
    ) -> ChannelRef:
        minutes = min(ttl_seconds // 60, MAX_SUBSCRIPTION_MINUTES)
        expires = datetime.now(UTC) + timedelta(minutes=minutes)
        body = {
            "changeType": "created,updated,deleted",
            "notificationUrl": channel_url,
            "lifecycleNotificationUrl": f"{channel_url}?lifecycle=1",
            "resource": "me/events",
            "expirationDateTime": expires.isoformat().replace("+00:00", "Z"),
            "clientState": token,
            "latestSupportedTlsVersion": "v1_2",
        }
        async with self._client() as client:
            data = _check(
                await client.post(f"{GRAPH}/subscriptions", json=body, headers=self._auth(token_set)),
                "subscription",
            )
        raw = data.get("expirationDateTime")
        if raw:
            expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return ChannelRef(channel_id, data.get("id"), expires)

    async def renew_watch(self, token_set: TokenSet, ref: ChannelRef, ttl_seconds: int) -> ChannelRef:
        minutes = min(ttl_seconds // 60, MAX_SUBSCRIPTION_MINUTES)
        expires = datetime.now(UTC) + timedelta(minutes=minutes)
        async with self._client() as client:
            _check(
                await client.patch(
                    f"{GRAPH}/subscriptions/{ref.resource_id}",
                    json={"expirationDateTime": expires.isoformat().replace("+00:00", "Z")},
                    headers=self._auth(token_set),
                ),
                "subscription renew",
            )
        return ChannelRef(ref.channel_id, ref.resource_id, expires)

    async def stop_watch(self, token_set: TokenSet, ref: ChannelRef) -> None:
        if not ref.resource_id:
            return
        async with self._client() as client:
            resp = await client.delete(
                f"{GRAPH}/subscriptions/{ref.resource_id}", headers=self._auth(token_set)
            )
        if resp.status_code == 404:
            return
        _check(resp, "subscription delete")

    async def list_changes(
        self,
        token_set: TokenSet,
        sync_token: str | None,
        *,
        known_ids: frozenset[str],
        known_ical_uids: frozenset[str],
        window: tuple[datetime, datetime],
    ) -> tuple[list[EventChange], str]:
        """CalendarView delta; only known ids / iCalUIds are fetched in detail (never subject or body)."""
        headers = {**self._auth(token_set), "Prefer": 'odata.maxpagesize=100, outlook.timezone="UTC"'}
        url: str | None
        params: dict[str, str] | None
        if sync_token:
            url, params = sync_token, None
        else:
            url = f"{GRAPH}/me/calendarView/delta"
            params = {
                "startDateTime": window[0].astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "endDateTime": window[1].astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
        out: list[EventChange] = []
        delta_link = sync_token or ""
        async with self._client() as client:
            for _ in range(50):
                if url is None:
                    break  # type: ignore[unreachable]
                resp = await client.get(url, params=params, headers=headers)
                params = None
                if resp.status_code == 410 or "syncStateNotFound" in resp.text[:2000]:
                    raise SyncTokenInvalid("microsoft delta: resync required")
                data = _check(resp, "delta")
                for item in data.get("value") or []:
                    event_id = str(item.get("id") or "")
                    ical = item.get("iCalUId")
                    if event_id not in known_ids and (ical or "") not in known_ical_uids:
                        continue  # not a Talent OS event: dropped here, never stored or logged
                    if "@removed" in item:
                        out.append(EventChange(event_id, ical, "deleted", None, None, None, None))
                        continue
                    detail = await client.get(
                        f"{GRAPH}/me/events/{event_id}",
                        params={
                            "$select": DETAIL_SELECT,
                            "$expand": f"singleValueExtendedProperties($filter=id eq '{TOS_TAG_PROP}')",
                        },
                        headers=headers,
                    )
                    if detail.status_code == 404:
                        out.append(EventChange(event_id, ical, "deleted", None, None, None, None))
                        continue
                    out.append(_graph_change(_check(detail, "event detail")))
                next_link = data.get("@odata.nextLink")
                if next_link:
                    url = str(next_link)
                    continue
                delta_link = str(data.get("@odata.deltaLink") or delta_link)
                break
        return out, delta_link


def _graph_addr(value: Any) -> str:
    return str(((value or {}).get("emailAddress") or {}).get("address") or "").lower()


def parse_graph_message(item: dict[str, Any], attachment_names: tuple[str, ...] = ()) -> InboundMessage:
    body = item.get("body") or {}
    content = str(body.get("content") or "")
    is_html = str(body.get("contentType") or "").lower() == "html"
    raw_time = str(item.get("receivedDateTime") or "")
    try:
        sent_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError:
        sent_at = datetime.now(UTC)
    return InboundMessage(
        provider_message_id=str(item.get("id")),
        rfc_message_id=item.get("internetMessageId") or None,
        in_reply_to=None,
        from_address=_graph_addr(item.get("from")),
        to=tuple(_graph_addr(r) for r in item.get("toRecipients") or []),
        cc=tuple(_graph_addr(r) for r in item.get("ccRecipients") or []),
        subject=str(item.get("subject") or ""),
        sent_at=sent_at,
        body_text="" if is_html else content,
        body_html=content if is_html else None,
        attachment_names=attachment_names,
    )


DETAIL_SELECT = "id,iCalUId,start,end,isCancelled,attendees,organizer,changeKey"
_GRAPH_RESPONSES = {
    "none": "needs_action",
    "notResponded": "needs_action",
    "organizer": "accepted",
    "accepted": "accepted",
    "tentativelyAccepted": "tentative",
    "declined": "declined",
}


def _graph_change(item: dict[str, Any]) -> EventChange:
    tag = None
    for prop in item.get("singleValueExtendedProperties") or []:
        if str(prop.get("id") or "").lower() == TOS_TAG_PROP.lower():
            tag = prop.get("value")
    attendees = tuple(
        AttendeeResponse(
            str((a.get("emailAddress") or {}).get("address") or "").lower(),
            _GRAPH_RESPONSES.get(str((a.get("status") or {}).get("response") or "none"), "needs_action"),  # type: ignore[arg-type]
        )
        for a in item.get("attendees") or []
        if (a.get("emailAddress") or {}).get("address")
    )
    return EventChange(
        provider_event_id=str(item.get("id")),
        ical_uid=item.get("iCalUId"),
        status="cancelled" if item.get("isCancelled") else "confirmed",
        start_utc=_parse_graph(item["start"]) if item.get("start") else None,
        end_utc=_parse_graph(item["end"]) if item.get("end") else None,
        etag=item.get("changeKey"),
        tos_tag=tag,
        attendees=attendees,
        organizer_is_self=True,
    )

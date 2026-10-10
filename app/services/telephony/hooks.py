# ruff: noqa: E501
"""Telephony wiring (Epic 76), autodiscovered by ``core.hooks``.

* the Connector Manifests of Exotel, Twilio, Plivo (``voice``) and Azure AI Speech (``stt``, D-6);
* the P4 hooks (D-22, additive): the ``voice`` inbound-event interceptor (``voice_calls`` lifecycle first) and the
  transcription resolver for uploaded recordings (STT jobs);
* after-commit task kicks and the ``ConsentWithdrawn`` handler (``handlers``);
* notification types ``telephony_cost_threshold``, ``telephony_cost_cap_reached`` (mandatory),
  ``telephony_provider_delete_failed`` and ``telephony_call_failed``;
* the settings hub entry and the audit labels.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession


def _manifests() -> None:
    from app.core.integrations import register_connector
    from app.services.connectors.stt.manifests import AZURE_SPEECH
    from app.services.connectors.voice.manifests import TELEPHONY_MANIFESTS

    for m in (*TELEPHONY_MANIFESTS, AZURE_SPEECH):
        register_connector(m)


def _p4_hooks() -> None:
    from app.services.telephony import ingress, stt_jobs
    from app.services.voice.provider_jobs import register_event_interceptor
    from app.services.voice.uploads import register_transcription_resolver

    register_event_interceptor(ingress.intercept)
    register_transcription_resolver(stt_jobs.resolver)


def _handlers() -> None:
    from app.services.telephony import handlers

    handlers.register()


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _notifications() -> None:
    from app.services import notifications_service as ns

    async def managers(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001
        from app.services.identity_bridge import users_with_permission

        return {u["id"] for u in await users_with_permission(db, "integration:manage")}

    async def to_user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001
        uid = _uuid(payload.get("user_id"))
        return {uid} if uid else set()

    async def threshold(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        return ns.Content(
            title=f"Calling budget {payload.get('pct', 0)}% used today",
            body="Today's telephony and transcription spend is close to the daily cap.",
            entity_type=None, entity_id=None, link_path="/settings/telephony",
        )  # fmt: skip

    async def cap(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        return ns.Content(
            title="Today's calling budget is used up",
            body="New calls are refused until tomorrow. Calls in progress continue. Recruiters can call outside Talent OS and upload the recording.",
            entity_type=None, entity_id=None, link_path="/settings/telephony",
        )  # fmt: skip

    async def delete_failed(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        return ns.Content(
            title=f"Couldn't delete the provider's copy of {payload.get('human_id') or 'a call'} recording",
            body="Delete the recording in the calling provider's console.",
            entity_type=None, entity_id=None, link_path="/settings/telephony",
        )  # fmt: skip

    async def call_failed(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        aid = _uuid(payload.get("application_id"))
        return ns.Content(
            title="Your call couldn't be placed",
            body="The calling provider didn't accept the call. You can call outside Talent OS and upload the recording.",
            entity_type="application" if aid else None, entity_id=aid,
            link_path=f"/applications/{aid}?panel=voice" if aid else None,
        )  # fmt: skip

    for key, label, channel, mandatory, recipients, render in (
        ("telephony_cost_threshold", "Calling budget alert", "both", False, managers, threshold),
        ("telephony_cost_cap_reached", "Calling budget used up", "both", True, managers, cap),
        ("telephony_provider_delete_failed", "Provider recording not deleted", "both", False, managers, delete_failed),
        ("telephony_call_failed", "Call couldn't be placed", "in_app", False, to_user, call_failed),
    ):
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(
                key, ns.NotificationType(label, "Integrations", channel, mandatory, recipients, render, internal_only=True)
            )


def _settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "integrations", "telephony", "Calling and transcription",
        "Caller IDs, calling hours, recording notice, daily budget and automatic transcription.",
        "/api/v1/settings/telephony", "integration:manage", owner_epic="Epic 76",
    )  # fmt: skip


def _audit() -> None:
    from app.core import audit

    for key, text in (
        ("telephony.call_placed", "A screening call was placed"),
        ("telephony.call_ended", "A screening call ended"),
        ("telephony.call_hangup", "A screening call was hung up"),
        ("telephony.do_not_call", "A candidate was marked do-not-call"),
        ("telephony.recording_stored", "A call recording was stored"),
        ("telephony.recording_skipped", "A call recording was not retrieved (no longer permitted)"),
        ("telephony.provider_recording_deleted", "The provider's copy of a call recording was deleted"),
        ("telephony.provider_delete_unsupported", "The provider does not support deleting call recordings"),
        ("telephony.call_phone_added", "A calling phone was added"),
        ("telephony.call_phone_verified", "A calling phone was verified"),
        ("telephony.call_phone_attested", "A calling phone was confirmed by an admin"),
        ("telephony.call_phone_removed", "A calling phone was removed"),
    ):
        audit.register_audit_action(key, text)


for _fn in (_manifests, _p4_hooks, _handlers, _notifications, _settings, _audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("telephony hooks: {} failed", _fn.__name__)
        raise

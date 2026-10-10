# ruff: noqa: E501
"""Job distribution wiring (Epic 70), autodiscovered by ``core.hooks``.

* the Connector Manifests ``naukri``, ``linkedin``, ``indeed``, ``job_feed`` (Epic 69 syncs the listings);
* the ``job_board`` sync handlers, the push-failure job listener and the Indeed Apply webhook handler;
* the platform LinkedIn app credentials (D-3) for the LinkedIn adapter (``vault_service.platform_get``);
* careers attribution resolvers (``src`` registry in ``portal.apply``, D-18);
* Public Events ``board_posting.published|status_changed|closed``; the ``job_board_application`` default notice;
* after-commit kicks (notice email, ``ConsentWithdrawn`` suppression); notification types; settings hub; audit labels.
"""

import uuid
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


def _manifests() -> None:
    from app.core.integrations import find_manifest, register_connector
    from app.services.connectors.job_board.manifests import JOB_BOARD_MANIFESTS

    for m in JOB_BOARD_MANIFESTS:
        if find_manifest(m.key) is None:
            register_connector(m)


def _runtime() -> None:
    from app.services.connectors import registry as connector_registry
    from app.services.job_distribution import attribution, ingest, sync

    sync.register()
    connector_registry.register_job_listener(sync.on_job_outcome)
    if connector_registry.webhook_handler("job_board") is None:
        connector_registry.register_webhook_handler("job_board", ingest.on_webhook)
    attribution.register()


def _linkedin() -> None:
    from app.services.connectors.job_board import linkedin

    async def creds() -> tuple[str, str]:
        from app.services import vault_service

        return (
            await vault_service.platform_get("linkedin_client_id", purpose="connector:linkedin"),
            await vault_service.platform_get("linkedin_client_secret", purpose="connector:linkedin"),
        )

    linkedin.set_platform_credentials(creds)


class BoardPostingData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    posting_id: uuid.UUID
    demand_id: uuid.UUID
    board_key: str
    status: str


def _public_events() -> None:
    from app.core.public_events import PublicEventSpec, all_specs, register_public_event
    from app.events.job_distribution import (
        BoardPostingClosed,
        BoardPostingPublished,
        BoardPostingStatusChanged,
    )

    def build(e: Any) -> BoardPostingData:
        return BoardPostingData(id=e.board_posting_id, posting_id=e.posting_id, demand_id=e.demand_id, board_key=e.board_key, status=getattr(e, "status", None) or e.to_status)

    known = {s.type for s in all_specs(include_internal=True)}
    for type_, source, desc in (
        ("board_posting.published", BoardPostingPublished, "A job went live on an external job board."),
        ("board_posting.status_changed", BoardPostingStatusChanged, "A board posting changed status (live, expiring, expired, closed, failed...)."),
        ("board_posting.closed", BoardPostingClosed, "A board posting was closed."),
    ):
        if type_ in known:
            continue
        register_public_event(
            PublicEventSpec(
                type=type_, version=1, source=source, subject_type="board_posting", subject_id=lambda e: e.board_posting_id,
                data_model=BoardPostingData, build=build, scopes=("postings:read",), owner_epic="70", description=desc,
            )
        )  # fmt: skip


def _notice() -> None:
    from app.services.job_distribution.notices import NOTICE_TITLE
    from app.services.privacy.consent import register_default_notice

    register_default_notice(
        "job_board_application", NOTICE_TITLE,
        "You applied to {tenant} for {job} through {board}. {tenant} now holds your application and CV to consider you "
        "for this and similar roles. You can see, correct or delete your data at {privacy link}.",
    )  # fmt: skip


def _after_commit() -> None:
    from app.core.events import subscribe
    from app.events.job_distribution import BoardApplicantIngested
    from app.events.privacy import ConsentWithdrawn

    def notice(e: BoardApplicantIngested) -> None:
        if not e.notify_email or e.application_id is None:
            return
        from app.tasks import job_distribution as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.send_notice, str(e.tenant_id), str(e.application_id), e.board_key)

    def withdrawn(e: ConsentWithdrawn) -> None:
        if e.purpose != "data_processing":
            return
        from app.tasks import job_distribution as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.suppress_withdrawn, str(e.tenant_id), str(e.candidate_id))

    subscribe(BoardApplicantIngested, notice)
    subscribe(ConsentWithdrawn, withdrawn)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _notifications() -> None:
    from app.services import notifications_service as ns

    async def to_user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001
        uid = _uuid(payload.get("user_id"))
        return {uid} if uid else set()

    async def failed(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        did = _uuid(payload.get("demand_id"))
        return ns.Content(
            title=f"Couldn't post the job on {payload.get('board') or 'a job board'}",
            body=(payload.get("message") or "The board did not accept the posting.") + " You can copy the posting pack and post it by hand.",
            entity_type="demand" if did else None, entity_id=did, link_path=f"/demands/{did}?panel=job_boards" if did else None,
        )  # fmt: skip

    async def expiring(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        did = _uuid(payload.get("demand_id"))
        return ns.Content(title=f"Board posting expiring on {payload.get('board') or 'a job board'}", body="Refresh it or close it.",
                          entity_type="demand" if did else None, entity_id=did, link_path=f"/demands/{did}?panel=job_boards" if did else None)  # fmt: skip

    async def digest(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        lines = list(payload.get("lines") or [])
        return ns.Content(title="New applicants from job boards", body="\n".join(lines)[:2000] or None, link_path="/reports/job-boards")

    async def ingest_failed(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        return ns.Content(title="A job board application couldn't be matched to a posting", body="Check the board's postings in Talent OS.", link_path="/settings/integrations")

    for key, label, channel, recipients, render in (
        ("board_posting_failed", "Job board posting failed", "both", to_user, failed),
        ("board_posting_expiring", "Job board posting expiring", "in_app", to_user, expiring),
        ("job_board_applicants_digest", "Daily job board applicants", "both", to_user, digest),
        ("board_ingest_failed", "Job board application not matched", "in_app", to_user, ingest_failed),
    ):
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, ns.NotificationType(label, "Integrations", channel, False, recipients, render, internal_only=True))


def _settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "integrations", "job_boards", "Job boards", "Default boards, field mappings, expiry reminders and the XML feed.",
        "/api/v1/job-boards/settings", "integration:manage", owner_epic="Epic 70",
    )  # fmt: skip


def _audit() -> None:
    from app.core import audit

    for key, text in (
        ("job_board.publish_requested", "A job was sent to a job board"),
        ("job_board.update_requested", "A job board posting update was requested"),
        ("job_board.refresh_requested", "A job board posting refresh was requested"),
        ("job_board.close_requested", "A job board posting close was requested"),
        ("job_board.pushed", "A job board accepted a posting"),
        ("job_board.recorded_manually", "A job board posting was recorded manually"),
        ("job_board.marked_closed", "A job board posting was marked closed"),
        ("job_board.applicant_suppressed", "A job board applicant was not created (erased or withdrawn)"),
    ):
        audit.register_audit_action(key, text)


for _fn in (_manifests, _runtime, _linkedin, _public_events, _notice, _after_commit, _notifications, _settings, _audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("job distribution hooks: {} failed", _fn.__name__)
        raise

"""Registrations of the ``documents`` domain (autodiscovered by ``core.hooks``), Epic 22 A10/A12.

* ``files_service``: access checker ``document`` (downloads re-run the document rule) and the P1 attachment
  store (``/{entity}/{id}/attachments`` served from documents).
* Notification types ``esign_requested``, ``esign_reminder``, ``esign_completed``, ``esign_declined``.
* Workflow triggers ``documents.signature_completed`` / ``documents.signature_declined`` and the action
  ``documents.send_signature_request`` (Epic 32 catalogue).
* Retention bindings (class ``signed_agreements``) for signed documents and their evidence.
* After-commit work (``EsignWork``) -> Celery tasks ``documents.*``.
"""

import uuid
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.services import files_service
from app.services.documents import repository
from app.services.documents.esign import EsignWork

# ---------------------------------------------------------------- files
files_service.register_access_checker("document", repository.file_access, plural="documents")
files_service.register_attachment_store(repository.AttachmentFacade())


# ---------------------------------------------------------------- notifications
def _register_notifications() -> None:
    from app.services import notifications_service as ns

    async def _nobody(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001
        return set()

    def _render(prefix: str) -> Any:
        async def render(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
            return ns.Content(title=f"{prefix}: {payload.get('title', '')}"[:300])

        return render

    for key, label, prefix in (
        ("esign_requested", "Signature requested", "Please sign"),
        ("esign_reminder", "Signature reminder", "Reminder: please sign"),
        ("esign_completed", "Signature completed", "Signed by everyone"),
        ("esign_declined", "Signature declined", "Signature declined"),
    ):
        if key in ns.NOTIFICATION_TYPES:
            continue
        ns.register_notification_type(
            key,
            ns.NotificationType(
                label,
                "Documents",
                "in_app",
                False,
                _nobody,
                _render(prefix),
                external_default_channel="in_app",
            ),
        )


_register_notifications()


# ---------------------------------------------------------------- workflow (Epic 32 catalogue)
class SendSignatureParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: uuid.UUID
    expires_in_days: int = 14


def _register_workflow() -> None:
    from app.services.workflow import actions, registry

    def _is(status: str) -> Any:
        def check(event: object) -> bool:
            return getattr(event, "to_status", None) == status

        return check

    for key, label, cls, matches in (
        ("documents.signature_completed", "Signature request completed", "SignatureRequestCompleted", None),
        (
            "documents.signature_declined",
            "Signature request declined",
            "SignatureRequestStatusChanged",
            _is("declined"),
        ),
    ):
        if key in registry.TRIGGERS:
            continue
        registry.register_trigger(
            key,
            registry.Trigger(
                key=key,
                label=label,
                event_module="app.events.documents",
                event_class=cls,
                entity_type="signature_request",
                entity_attr="request_id",
                matches=matches,
            ),
        )

    def _validate(params: Any, entity_type: str, path: str) -> None:  # noqa: ARG001
        if entity_type != "candidate":
            raise actions._invalid(path, "only for candidate triggers")

    async def _run(ac: Any) -> str:
        from datetime import timedelta

        from app.services.documents import esign
        from app.services.documents.common import now

        req = await esign.create_request(
            ac.ctx,
            ac.db,
            owner_kind="candidate",
            owner_id=ac.entity_id,
            signers=[esign.SignerInput("candidate", ac.entity_id)],
            template_id=ac.params.template_id,
            merge_data={"worker": {}},
            expires_at=now() + timedelta(days=ac.params.expires_in_days),
            send=True,
            check_access=False,
        )
        return f"Sent signature request {req.human_id}"

    async def _dry(ac: Any) -> str:  # noqa: ARG001
        return "Would send a signature request to the candidate"

    if "documents.send_signature_request" not in actions.ACTIONS:
        actions.register_action(
            "documents.send_signature_request",
            actions.ActionDef("Send for signature", SendSignatureParams, _validate, _run, _dry),
        )


try:
    _register_workflow()
except Exception:  # pragma: no cover - never block start-up on the optional catalogue
    logger.exception("documents: workflow catalogue registration failed")


# ---------------------------------------------------------------- retention (AD-34, R15)
def _register_retention() -> None:
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    async def _subject(db: AsyncSession, row_id: uuid.UUID) -> uuid.UUID | None:
        from app.models.documents import Document

        doc = await db.get(Document, row_id)
        return doc.worker_candidate_id if doc else None

    async def _anchor(db: AsyncSession, row_id: uuid.UUID) -> Any:
        from sqlalchemy import func, select

        from app.models.documents import Signature, SignatureRequest

        return (
            await db.execute(
                select(func.max(Signature.signed_at))
                .join(SignatureRequest, SignatureRequest.id == Signature.request_id)
                .where((SignatureRequest.document_id == row_id) | (SignatureRequest.id == row_id))
            )
        ).scalar()

    async def _expire(db: AsyncSession, ctx: Any) -> Any:
        from app.services.documents_erasure import erase_unretained

        return await erase_unretained(db, ctx, include_signed=True)

    existing = retention_bindings()
    for table, pii in (
        ("documents", ("title", "note")),
        ("document_versions", ()),
        ("signatures", ("typed_name", "email", "ip", "user_agent")),
        ("signature_events", ("ip", "user_agent")),
    ):
        if table not in existing:
            register_retention_binding(
                table,
                "signed_agreements",
                subject_resolver=_subject,
                anchor_resolver=_anchor,
                expire_fn=_expire,
                pii_columns=pii,
            )


_register_retention()


# ---------------------------------------------------------------- after-commit work
def _enqueue(task_name: str, *args: object) -> None:
    import app.tasks.documents  # noqa: F401 - registers the tasks
    from app.core.celery import celery_app
    from app.tasks.runtime import enqueue

    enqueue(celery_app.tasks[task_name], *args)


def _on_work(e: EsignWork) -> None:
    if e.kind in ("notify", "remind"):
        _enqueue("documents.notify_signer", str(e.tenant_id), str(e.signer_id), e.kind == "remind")
    elif e.kind == "complete":
        _enqueue("documents.complete_request", str(e.tenant_id), str(e.request_id))
    elif e.kind == "declined":
        _enqueue("documents.notify_declined", str(e.tenant_id), str(e.request_id))


subscribe(EsignWork, _on_work)

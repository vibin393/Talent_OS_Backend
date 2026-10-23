# ruff: file-ignore[line-too-long, invalid-formatter-suppression-comment]
"""Public API registrations (Epic 67, spec A7): the import kick-off (``PublicImportQueued`` -> ``public_api.start_import``
after commit), the ``bulk_import.completed`` public event and the notification types ``api_deprecated_usage`` and
``public_import_finished``."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.core.public_events import PublicEventSpec, all_specs, register_public_event
from app.events.public_api import PublicApiDeprecatedUsage, PublicImportCompleted, PublicImportQueued


def _enqueue_import(event: PublicImportQueued) -> None:
    from app.tasks.public_api import start_import
    from app.tasks.runtime import enqueue

    enqueue(start_import, str(event.tenant_id), str(event.import_id))


subscribe(PublicImportQueued, _enqueue_import)


class BulkImportCompletedData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_id: uuid.UUID
    resource: str
    status: str
    created: int
    updated: int
    duplicates: int
    failed: int


def _bulk_import_completed(e: PublicImportCompleted) -> BulkImportCompletedData:
    return BulkImportCompletedData(import_id=e.import_id, resource=e.resource, status=e.status, created=e.created,
                                   updated=e.updated, duplicates=e.duplicates, failed=e.failed)  # fmt: skip


def _register_event() -> None:
    if any(s.type == "bulk_import.completed" for s in all_specs(include_internal=True)):
        return
    register_public_event(
        PublicEventSpec(
            type="bulk_import.completed", version=1, source=PublicImportCompleted, subject_type="import",
            subject_id=lambda e: e.import_id, data_model=BulkImportCompletedData, build=_bulk_import_completed,
            scopes=("imports:write",), owner_epic="67", description="A Public API bulk import finished.",
        )
    )  # fmt: skip


_register_event()


def _register_notifications() -> None:
    from sqlalchemy import select, text

    from app.services import notifications_service as ns

    async def deprecated_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        from app.models.api_keys import ApiKey
        from app.models.oauth import AppInstallation

        out: set[uuid.UUID] = set()
        if payload.get("installation_id"):
            owner = (await db.execute(select(AppInstallation.accountable_user_id).where(AppInstallation.id == uuid.UUID(str(payload["installation_id"]))))).scalar_one_or_none()
            if owner:
                out.add(owner)
        if payload.get("api_key_id"):
            creator = (await db.execute(select(ApiKey.created_by).where(ApiKey.id == uuid.UUID(str(payload["api_key_id"]))))).scalar_one_or_none()
            if creator:
                out.add(creator)
        rows = await db.execute(
            text(
                "SELECT DISTINCT ur.user_id FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
                "JOIN users u ON u.id = ur.user_id WHERE u.is_active AND (r.key = 'tenant_admin' "
                "OR EXISTS (SELECT 1 FROM jsonb_object_keys(r.permissions) k WHERE k = 'integration:manage'))"
            )
        )
        out.update(r[0] for r in rows)
        return out

    async def deprecated_render(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return ns.Content(
            title="Your integration uses an API that is being retired",
            body=f"{payload.get('route_template')} was called {payload.get('call_count')} times last month. It stops working on {payload.get('sunset')}.",
            entity_type="installation" if payload.get("installation_id") else None,
            entity_id=uuid.UUID(str(payload["installation_id"])) if payload.get("installation_id") else None,
        )

    async def import_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        from app.models.public_api import PublicImportJob

        user = (await db.execute(select(PublicImportJob.acting_user_id).where(PublicImportJob.id == uuid.UUID(str(payload["import_id"]))))).scalar_one_or_none()
        return {user} if user else set()

    async def import_render(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return ns.Content(
            title="Your bulk import finished",
            body=f"{payload.get('created', 0)} created, {payload.get('duplicates', 0)} possible duplicates, {payload.get('failed', 0)} failed.",
        )

    if "api_deprecated_usage" not in ns.NOTIFICATION_TYPES:
        ns.register_notification_type(
            "api_deprecated_usage",
            ns.NotificationType("Your integration uses an API that is being retired", "Integrations", "both", False,
                                deprecated_recipients, deprecated_render, internal_only=True),  # fmt: skip
        )
        ns.register_event_source(PublicApiDeprecatedUsage, lambda p: "api_deprecated_usage")  # ruff: ignore[unused-lambda-argument]
    if "public_import_finished" not in ns.NOTIFICATION_TYPES:
        ns.register_notification_type(
            "public_import_finished",
            ns.NotificationType("Bulk import finished", "Integrations", "both", False, import_recipients, import_render,
                                internal_only=True),  # fmt: skip
        )
        ns.register_event_source(PublicImportCompleted, lambda p: "public_import_finished")  # ruff: ignore[unused-lambda-argument]


_register_notifications()

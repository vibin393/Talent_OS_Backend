"""Portal notification preferences of External Principals (Epic 35 A7, FR-184; migration 20261005_222_03).

The P1 ``notification_preferences`` rows are keyed by ``user_id`` (FK to ``users``); External Principals
(``candidate`` / ``client_user`` / ``vendor_user``) keep their per-type email switch here instead (Epic 35
deviation: a separate table rather than relaxing the P1 FK).
"""

import uuid

from sqlalchemy import Boolean, CheckConstraint, String, UniqueConstraint, Uuid, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

PORTAL_RECIPIENT_KINDS = ("candidate", "client_user", "vendor_user")


class PortalNotificationPreference(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "portal_notification_preferences"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "recipient_kind", "recipient_id", "type", name="uq_portal_notif_prefs_recipient_type"
        ),
        CheckConstraint(
            "recipient_kind IN ('candidate', 'client_user', 'vendor_user')", name="recipient_kind"
        ),
    )

    recipient_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    email: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())

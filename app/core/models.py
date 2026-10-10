"""Declarative base and mixins shared by every module (see architecture Consistency Conventions)."""

import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[type, object]] = {
        datetime: DateTime(timezone=True),
        uuid.UUID: Uuid(as_uuid=True),
    }


class UUIDPk:
    """uuid4 primary key generated app-side (AD-3)."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Timestamps:
    """``created_at`` / ``updated_at`` as timestamptz (AD-14)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TenantMixin:
    """Marks a tenant-owned table. Its migration MUST call ``enable_rls(<table>)`` (AD-2).

    ``tenant_id`` references ``tenants.id`` (platform table, story 1.2); tenants are never hard-deleted
    while they own data (``ON DELETE RESTRICT``).
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )


class AuditedMixin:
    """Models inheriting this are audited by the ``before_flush`` listener in ``app.core.audit`` (AD-5).

    * ``__pii_fields__``: values stored encrypted with the subject's data key (crypto-shredding); the
      subject is ``(__pii_subject__ or entity type, entity id)``.
    * ``__audit_exclude__``: columns never audited (``updated_at`` and names containing
      password/secret/token/hash are always excluded).
    * ``__audit_entity__`` / ``__audit_entity_id__``: override the entity type (default: snake_case class
      name) and the attribute used as entity id (default ``id``; composite-key tables name one column).
    """

    __audited__: ClassVar[bool] = True
    __pii_fields__: ClassVar[tuple[str, ...]] = ()
    __audit_exclude__: ClassVar[tuple[str, ...]] = ()

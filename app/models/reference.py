"""Platform reference tables (AD-17): GST state codes and ISO-4217 currencies.

Non-RLS platform tables (listed in ``core.tenancy.PLATFORM_TABLES``), seeded by migration 20261002_200_01 from
``app/core/data/gst_states.json`` / ``currencies.json``; ``talent_app`` has SELECT only.
"""

from sqlalchemy import CHAR, Boolean, CheckConstraint, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base

GST_STATE_KINDS = ("state", "ut_legislature", "ut_no_legislature", "other_territory", "other_country")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class GstState(Base):
    __tablename__ = "gst_states"
    __table_args__ = (CheckConstraint(_in("kind", GST_STATE_KINDS), name="kind"),)

    code: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)


class Currency(Base):
    __tablename__ = "currencies"
    __table_args__ = (CheckConstraint("minor_units BETWEEN 0 AND 4", name="minor_units"),)

    code: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    minor_units: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    document_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)

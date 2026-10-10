"""Client Portal schema of the commercials domain (FR-80 framework): PO consumption as the client sees it.
No internal fields (owner, notes, exception approvals, documents) are exposed."""

from datetime import date

from pydantic import BaseModel, ConfigDict

from app.core.money import DecimalStr


class ClientPoConsumptionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    po_number: str
    currency: str
    value_cap: DecimalStr | None
    hours_cap: DecimalStr | None
    consumed_amount: DecimalStr
    consumed_hours: DecimalStr
    remaining_amount: DecimalStr | None
    remaining_hours: DecimalStr | None
    valid_to: date

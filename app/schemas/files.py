import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

AttachmentCategory = Literal[
    "certificate", "id_proof", "offer_letter", "relieving_letter", "payslip", "other"
]


class StoredFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_name: str
    content_type: str
    size_bytes: int
    sha256: str
    scan_status: str
    created_at: datetime
    download_url: str


class AttachmentOut(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    category: AttachmentCategory
    expires_on: date | None
    note: str | None
    archived_at: datetime | None
    uploaded_by: uuid.UUID | None
    created_at: datetime
    file: StoredFileOut

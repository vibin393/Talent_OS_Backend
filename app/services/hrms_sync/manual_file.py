# ruff: file-ignore[line-too-long]
"""The Manual HRMS import file (Epic 71, story 71.1, spec A3): "Download HRMS import file" builds a CSV (UTF-8 with
BOM) with one row per eligible ``pending`` / ``blocked`` record of the Installation x Legal Entity, columns = the
mapped HRMS field keys in schema order (custom keys after), using the same mapping (sensitive fields only when
enabled). Stored as a generated file and audited (no values in the audit row)."""

import csv
import io
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.models.hrms_sync import HrmsSyncRecord
from app.models.placements import Placement
from app.services import files_service
from app.services.hrms_sync import mappings
from app.services.hrms_sync.catalogue import placement_values
from app.services.hrms_sync.common import get_settings_row, require_installation
from app.services.hrms_sync.eligibility import eligible

__all__ = ["FILE_OWNER", "build_import_file"]

FILE_OWNER = "hrms_import_file"


async def build_import_file(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> files_service.Download:
    inst = await require_installation(db, installation_id, "hrms")
    schema = await mappings.schema_for(ctx, db, installation_id, legal_entity_id)
    rows = await mappings.load_rows(db, installation_id, legal_entity_id)
    order = [f.key for f in schema.fields]
    mapped = {r.hrms_field for r in rows}
    header = [k for k in order if k in mapped] + sorted(mapped - set(order))
    settings = await get_settings_row(db)
    recs = list(
        (
            await db.execute(
                select(HrmsSyncRecord)
                .where(HrmsSyncRecord.installation_id == installation_id, HrmsSyncRecord.legal_entity_id == legal_entity_id, HrmsSyncRecord.status.in_(("pending", "blocked")))
                .order_by(HrmsSyncRecord.created_at)
            )
        ).scalars()
    )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    count = 0
    wanted = mappings.wanted_keys(rows)
    for rec in recs:
        p = await db.get(Placement, rec.placement_id)
        if p is None or not eligible(p, settings, rec)[0]:
            continue
        values = await placement_values(db, p, wanted)
        fields, _missing = mappings.build_fields(rows, values, [])
        writer.writerow(["" if fields.get(k) is None else (fields[k].isoformat() if isinstance(fields[k], date) else str(fields[k])) for k in header])
        count += 1
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    filename = f"hrms-import-{inst.connector_key}-{date.today():%Y%m%d}.csv"
    await files_service.store_generated(
        ctx, db, data, key=storage.new_key(inst.tenant_id), filename=filename, content_type="text/csv",
        owner_entity_type=FILE_OWNER, owner_entity_id=installation_id,
    )  # fmt: skip
    audit.record("hrms.import_file_generated", "app_installation", installation_id, session=db, legal_entity_id=str(legal_entity_id), rows=count)
    return files_service.Download(data=data, filename=filename, content_type="text/csv")

"""Subject access export (Epic 12 A3.1 ``access``): every registry ``export()`` result plus the candidate's
files, zipped to ``t/<tid>/dsr/<id>.zip`` and delivered through a ``dsr_export`` secure link (7 days by
default, 3 uses). Target: completed within 24 h of ``execute``.

Also serves public downloads of export links (``dsr_export``, ``tenant_export``).
"""

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from app.core import audit, secure_links, storage
from app.core.database import tenant_session
from app.core.mailer import EmailMessage, send_email
from app.models.privacy import DsrRequest
from app.services import settings_service
from app.services.candidates import privacy_port as candidates
from app.services.privacy import registry
from app.services.privacy.erasure import SessionFactory

EXPORT_PURPOSE = "dsr_export"
EXPORT_MAX_USES = 3


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def export_key(tenant_id: uuid.UUID, dsr_id: uuid.UUID) -> str:
    return f"t/{tenant_id}/dsr/{dsr_id}.zip"


async def build_subject_export(
    tenant_id: uuid.UUID, dsr_id: uuid.UUID, *, sessions: SessionFactory = tenant_session
) -> str:
    """Build the zip, issue the link, email it and complete the DSR. Idempotent (completed -> no-op)."""
    async with sessions(tenant_id) as db:
        dsr = await db.get(DsrRequest, dsr_id)
        if dsr is None or dsr.status == "completed" or dsr.candidate_id is None:
            return "skipped"
        candidate_id = dsr.candidate_id
        requester_email = dsr.requester_email
        ectx = registry.ErasureContext(tenant_id=tenant_id, candidate_id=candidate_id, run_id=dsr_id, reason="dsr")
        sections: dict[str, Any] = {}
        for handler in registry.handlers():
            data = await handler.export(db, ectx)
            if data is not None:
                sections[handler.name] = data
        facts = (await candidates.basics(db, [candidate_id])).get(candidate_id) or {}
    files: list[dict[str, Any]] = [f for data in sections.values() for f in data.pop("_files", [])]

    buf = io.BytesIO()
    backend = storage.get_storage()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", json.dumps(sections, indent=2, default=_json_default, ensure_ascii=False))
        for index, file in enumerate(files, start=1):
            try:
                content = await backend.get(file["storage_key"])
            except Exception:
                logger.warning("Export: file {} missing from storage", file.get("id"))
                continue
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(file.get("name") or "file"))
            zf.writestr(f"files/{index:03d}_{safe[:80]}", content)
    key = export_key(tenant_id, dsr_id)
    await backend.put(key, buf.getvalue(), "application/zip")

    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        ttl = await settings_service.link_ttl(db, EXPORT_PURPOSE)
        link, token = await secure_links.issue(
            db,
            tenant_id=tenant_id,
            purpose=EXPORT_PURPOSE,
            subject_type="candidate",
            subject_id=candidate_id,
            ttl=ttl,
            max_uses=EXPORT_MAX_USES,
            metadata={"storage_key": key, "dsr_id": str(dsr_id), "filename": f"data-export-{dsr_id}.zip"},
        )
        from app.services.privacy import dsr as dsr_service

        await dsr_service.complete_system(db, dsr_id, {"export_link_id": str(link.id), "files": len(files)})
        audit.record("dsr.export_ready", "dsr_request", dsr_id, session=db, link_id=link.id)
    recipient = requester_email or facts.get("primary_email")
    if recipient:
        await send_email(
            EmailMessage(
                to=str(recipient),
                subject="Your personal data export is ready",
                text=(
                    "Hello,\n\nThe copy of your personal data you requested is ready. Download it here:\n"
                    f"{secure_links.link_url(token)}\n\nThe link can be used {EXPORT_MAX_USES} times and expires "
                    f"in {ttl.days} days.\n"
                ),
                tenant_id=tenant_id,
                kind="dsr_export",
            )
        )
    return "completed"


async def download(token: str) -> tuple[bytes, str]:
    """Public download of an export link (consumes one use). Returns ``(content, filename)``."""
    link = await secure_links.resolve(token, ("dsr_export", "tenant_export"))
    async with tenant_session(link.tenant_id) as db:
        await secure_links.consume(db, link.id)
    key = str(link.meta.get("storage_key") or "")
    content = await storage.get_storage().get(key)
    return content, str(link.meta.get("filename") or "export.zip")

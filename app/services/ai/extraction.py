"""AI commercial-document extraction (Epic 35 A5, FR-182, AD-6).

``extract_commercial_document(ctx, db, file_id, doc_kind)`` reads a scanned stored file, caps it at
``AI_EXTRACTION_MAX_PAGES`` pages, and runs the gateway with feature ``document_extraction`` (default off) and
``subject=None`` -- client documents have no candidate subject, so the consent check does not apply and the
action records ``meta.subject = "none"``. The result is a **draft only**: nothing is persisted outside
``ai_actions``; the commercials UI shows the fields as "AI-extracted" until a person confirms them.
"""

import io
import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.settings import get_settings
from app.schemas.ai import CommercialExtraction, CommercialExtractOut
from app.services.ai import gateway

FEATURE = "document_extraction"
PROMPT = "commercial_doc_extraction.v1"
MAX_CHARS = 120_000


def _pdf_text(data: bytes, max_pages: int) -> str | None:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return None
        parts = [(page.extract_text() or "") for page in list(reader.pages)[:max_pages]]
    except Exception as exc:  # malformed files raise all sorts of errors
        logger.info("commercial extraction: PDF unreadable ({})", type(exc).__name__)
        return None
    return "\n".join(parts).strip() or None


def document_text(data: bytes, content_type: str, max_pages: int) -> str | None:
    if content_type == "application/pdf":
        return _pdf_text(data, max_pages)
    if content_type.startswith("text/"):
        return data.decode("utf-8", "replace").strip() or None
    if "wordprocessingml" in content_type:
        from app.services.candidates.extraction import extract_text

        try:
            return extract_text(data, "docx").text
        except Exception:
            return None
    return None


async def extract_commercial_document(
    ctx: RequestContext, db: AsyncSession, file_id: uuid.UUID, doc_kind: str
) -> CommercialExtractOut:
    from app.services import files_service

    if not await gateway.is_feature_enabled(db, FEATURE):
        raise Forbidden("Document extraction is not enabled", code="feature_disabled")
    row = await files_service.get_file(db, file_id)
    if row is None or getattr(row, "deleted_at", None) is not None:
        raise NotFound("File not found")
    if row.scan_status not in ("clean", "skipped_dev"):
        raise ValidationFailed("The file has not passed the malware scan", code="file_not_scanned")
    try:
        data = await files_service.read_file(row)
    except FileNotFoundError as exc:
        raise NotFound("File not found") from exc
    text = document_text(data, row.content_type, get_settings().ai_extraction_max_pages)
    if not text:
        return CommercialExtractOut(ok=False, action_id=None, error_code="unreadable")
    result = await gateway.run(
        ctx,
        db,
        FEATURE,
        PROMPT,
        inputs={"doc_kind": doc_kind},
        untrusted={"document": text[:MAX_CHARS]},
        output_schema=CommercialExtraction,
        subject=None,
        input_ref=f"stored_file:{file_id}",
    )
    if not result.ok or result.data is None:
        return CommercialExtractOut(
            ok=False, action_id=result.action_id, error_code=result.error_code or "ai_failed"
        )
    draft = (
        result.data
        if isinstance(result.data, CommercialExtraction)
        else CommercialExtraction.model_validate(result.data.model_dump())
    )
    return CommercialExtractOut(ok=True, action_id=result.action_id, draft=draft)

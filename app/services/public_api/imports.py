# ruff: noqa: E501
"""Public bulk import (Epic 67, spec A4 "Bulk import", NFR-65).

``POST /imports`` (multipart ``file`` + ``resource`` + ``format``): size / row caps (``public_api_bulk_max_rows``; the
Sandbox cap), malware scan (``core.scanning``; infected -> the job is ``failed``), tenant storage, a
``public_import_jobs`` row and ``PublicImportQueued`` (after commit -> ``public_api.start_import``, queue
``interactive``). The task replays every row through the same domain functions as the single-record API
(``candidates.records.create_candidate`` with the P1 duplicate check; ``pipeline.applications.add_candidates``) as the
importing principal, counts ``created`` / ``duplicates`` / ``failed``, stores an error report CSV (row number, field and
message only) and emits ``PublicImportCompleted`` (public event ``bulk_import.completed``).

Candidate imports carry the consent basis of the whole file (``consent_basis``), as the single-record API does.
"""

import csv
import io
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, scanning, storage
from app.core.context import RequestContext, reset_context, set_context
from app.core.database import tenant_session
from app.core.errors import DomainError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.oauth_scopes import permissions_for
from app.core.permissions import CATALOG, Principal, Scope
from app.core.public_api import scope_satisfied
from app.core.settings import get_settings
from app.core.tenancy import is_sandbox
from app.events.public_api import PublicImportCompleted, PublicImportQueued
from app.models.oauth import AppInstallation
from app.models.public_api import PublicImportJob
from app.services.public_api._core import ctx_principal

CANDIDATE_COLUMNS = ("full_name", "email", "phone", "current_title", "current_employer", "owner_id", "source_detail")
APPLICATION_COLUMNS = ("candidate_id", "demand_id", "source_detail")
CONSENT_BASES = ("notice_provided", "consent_captured")
CHUNK = 100


def _now() -> datetime:
    return datetime.now(UTC)


def parse_rows(data: bytes, fmt: str) -> list[dict[str, Any]]:
    """Pure: the file's rows (422 ``invalid_file`` when it cannot be read)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("The file must be UTF-8", code="invalid_file") from exc
    if fmt == "csv":
        return [dict(r) for r in csv.DictReader(io.StringIO(text))]
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationFailed(f"Line {n} is not valid JSON", code="invalid_file") from exc
        if not isinstance(obj, dict):
            raise ValidationFailed(f"Line {n} is not a JSON object", code="invalid_file")
        rows.append(obj)
    return rows


def job_out(job: PublicImportJob) -> dict[str, Any]:
    return {
        "id": job.id, "object": "import", "resource": job.resource, "format": job.format, "status": job.status,
        "row_count": job.row_count, "batch_id": job.batch_id,
        "counts": {k: int(v) for k, v in (job.counts or {}).items() if isinstance(v, int)},
        "has_error_report": job.error_report_key is not None, "created_at": job.created_at, "completed_at": job.completed_at,
    }  # fmt: skip


async def create_import(db: AsyncSession, *, data: bytes, filename: str | None, resource: str, fmt: str, consent_basis: str | None) -> dict[str, Any]:
    s = get_settings()
    ctx, principal = ctx_principal()
    if resource not in ("candidates", "applications"):
        raise ValidationFailed("resource must be candidates or applications", details={"fields": {"resource": "candidates | applications"}})
    if fmt not in ("csv", "jsonl"):
        raise ValidationFailed("format must be csv or jsonl", details={"fields": {"format": "csv | jsonl"}})
    from app.core.public_api import insufficient_scope

    write_scope = f"{resource}:write"
    if not scope_satisfied(principal, write_scope):
        raise insufficient_scope([write_scope])
    if resource == "candidates" and consent_basis not in CONSENT_BASES:
        raise ValidationFailed("A consent basis is required to import candidates", code="consent_basis_required",
                               details={"fields": {"consent_basis": " | ".join(CONSENT_BASES)}})  # fmt: skip
    if len(data) > s.public_api_bulk_max_file_bytes:
        raise ValidationFailed("The file is too large", code="file_too_large", details={"max_bytes": s.public_api_bulk_max_file_bytes})
    rows = parse_rows(data, fmt)
    if len(rows) > s.public_api_bulk_max_rows:
        raise ValidationFailed(f"At most {s.public_api_bulk_max_rows} rows per import", code="too_many_rows",
                               details={"max_rows": s.public_api_bulk_max_rows, "rows": len(rows)})  # fmt: skip
    assert ctx.tenant_id is not None
    if await is_sandbox(ctx.tenant_id) and len(rows) > s.sandbox_import_cap:
        raise ValidationFailed(f"Sandbox imports are capped at {s.sandbox_import_cap} rows", code="sandbox_import_cap",
                               details={"max_rows": s.sandbox_import_cap})  # fmt: skip
    result = await scanning.scan(data, filename or f"import.{fmt}")
    key = storage.new_key(ctx.tenant_id)
    job = PublicImportJob(
        id=uuid.uuid4(), tenant_id=ctx.tenant_id,
        installation_id=principal.app_installation_id if principal.type == "app" else None,
        api_key_id=principal.api_key_id if principal.type == "api_key" else None,
        acting_user_id=principal.acting_user_id if principal.type == "app" else None,
        resource=resource, format=fmt, file_key=key, row_count=len(rows), status="queued",
        counts={"consent_basis": consent_basis} if consent_basis else {},
    )  # fmt: skip
    if result.status not in ("clean", "skipped_dev"):
        job.status, job.completed_at = "failed", _now()
        job.counts = {"created": 0, "updated": 0, "duplicates": 0, "failed": len(rows)}
    else:
        await storage.get_storage().put(key, data, "text/csv" if fmt == "csv" else "application/x-ndjson")
    db.add(job)
    await db.flush()
    audit.record("public_api.import_started", "public_import_job", job.id, session=db, resource=resource, format=fmt,
                 rows=len(rows), scan=result.status)  # fmt: skip
    if job.status == "queued":
        emit(db, PublicImportQueued(tenant_id=ctx.tenant_id, import_id=job.id))
    return job_out(job)


async def _job(db: AsyncSession, import_id: uuid.UUID) -> PublicImportJob:
    _, principal = ctx_principal()
    stmt = select(PublicImportJob).where(PublicImportJob.id == import_id)
    if principal.type == "app":
        stmt = stmt.where(PublicImportJob.installation_id == principal.app_installation_id)
    else:
        stmt = stmt.where(PublicImportJob.api_key_id == principal.api_key_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Import not found")
    return row


async def get_import(db: AsyncSession, import_id: uuid.UUID) -> dict[str, Any]:
    return job_out(await _job(db, import_id))


async def find_by_batch(db: AsyncSession, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    stmt = select(PublicImportJob).where(PublicImportJob.batch_id == batch_id)
    if principal.type == "app":
        stmt = stmt.where(PublicImportJob.installation_id == principal.app_installation_id)
    else:
        stmt = stmt.where(PublicImportJob.api_key_id == principal.api_key_id)
    return [job_out(j) for j in (await db.execute(stmt)).scalars()]


async def error_report(db: AsyncSession, import_id: uuid.UUID) -> bytes:
    job = await _job(db, import_id)
    if job.error_report_key is None:
        return b"row,field,error\n"
    return await storage.get_storage().get(job.error_report_key)


# ================================================================ processing (task body)
async def _principal_for(db: AsyncSession, job: PublicImportJob) -> Principal | None:
    """Rebuild the importing principal: the Installation's Grant (and the delegated user's own rights) or the key."""
    from app.services import entity_refs, oauth_service

    wanted = {"imports:write", f"{job.resource}:write", "candidates:read"}
    if job.installation_id is not None:
        inst = await db.get(AppInstallation, job.installation_id)
        if inst is None or inst.status not in ("active", "degraded"):
            return None
        scopes_ = frozenset(set(inst.granted_scopes or []) & wanted)
        if job.acting_user_id is not None:
            user = await entity_refs.principal_for_user(db, job.tenant_id, job.acting_user_id)
            if user is None:
                return None
            perms = oauth_service.delegated_permissions(user.permissions, scopes_)
            team_ids, bu = user.team_ids, user.business_unit_id
        else:
            perms, team_ids, bu = dict.fromkeys(permissions_for(scopes_), Scope.TENANT), frozenset(), None
        return Principal(type="app", id=inst.id, tenant_id=job.tenant_id, permissions=perms, team_ids=team_ids,
                         business_unit_id=bu, app_id=inst.app_id, app_installation_id=inst.id,
                         acting_user_id=job.acting_user_id, oauth_scopes=scopes_, resource="api")  # fmt: skip
    from app.models.api_keys import ApiKey

    key = await db.get(ApiKey, job.api_key_id)
    if key is None or key.revoked_at is not None:
        return None
    return Principal(type="api_key", id=key.id, tenant_id=job.tenant_id, api_key_id=key.id,
                     permissions={p: Scope.TENANT for p in key.scopes if p in CATALOG})  # fmt: skip


async def _row_candidate(db: AsyncSession, row: dict[str, Any], basis: str) -> str:
    from app.schemas.public_v1.resources import CandidateCreateIn, ConsentIn
    from app.services.public_api import candidates

    data = CandidateCreateIn(
        **{k: (row.get(k) or None) for k in CANDIDATE_COLUMNS if k != "full_name"},
        full_name=str(row.get("full_name") or "").strip(),
        consent=ConsentIn(basis=basis, captured_at=_now(), channel="bulk_import"),  # type: ignore[arg-type]
    )  # fmt: skip
    out = await candidates.create_candidate(db, data)
    return "duplicate" if out.get("possible_duplicate") else "created"


async def _row_application(db: AsyncSession, row: dict[str, Any]) -> str:
    from app.core.errors import ConflictError
    from app.schemas.public_v1.resources import ApplicationCreateIn
    from app.services.public_api import applications

    try:
        await applications.create_application(db, ApplicationCreateIn(
            candidate_id=uuid.UUID(str(row.get("candidate_id"))), demand_id=uuid.UUID(str(row.get("demand_id"))),
            source_detail=row.get("source_detail") or None,
        ))  # fmt: skip
    except ConflictError as exc:
        if exc.code == "application_exists":
            return "duplicate"
        raise
    return "created"


def _error_field(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, DomainError):
        fields = (exc.details or {}).get("fields") if isinstance(exc.details, dict) else None
        field = next(iter(fields)) if isinstance(fields, dict) and fields else ""
        return field, exc.code
    if hasattr(exc, "errors"):
        errs = exc.errors()  # type: ignore[attr-defined]
        loc = errs[0].get("loc", ("",)) if errs else ("",)
        return str(loc[-1] if loc else ""), "validation_failed"
    return "", "invalid_row"


async def process_import(tenant_id: uuid.UUID, import_id: uuid.UUID) -> dict[str, int]:
    """Celery task body: replay every row (chunked transactions, one savepoint per row)."""
    async with tenant_session(tenant_id) as db:
        job = await db.get(PublicImportJob, import_id)
        if job is None or job.status != "queued":
            return {}
        job.status = "running"
        principal = await _principal_for(db, job)
        snapshot = (job.resource, job.format, job.file_key, str((job.counts or {}).get("consent_basis") or "notice_provided"))
    resource, fmt, key, basis = snapshot
    counts = {"created": 0, "updated": 0, "duplicates": 0, "failed": 0}
    errors: list[tuple[int, str, str]] = []
    status = "completed"
    if principal is None:
        status = "failed"
        rows: list[dict[str, Any]] = []
    else:
        rows = parse_rows(await storage.get_storage().get(key), fmt)
    token = set_context(RequestContext(tenant_id=tenant_id, principal=principal))
    try:
        for start in range(0, len(rows), CHUNK):
            # One transaction per chunk; each row in its own savepoint (a failed row never undoes the others).
            async with tenant_session(tenant_id) as db:
                for n, row in enumerate(rows[start : start + CHUNK], start + 1):
                    try:
                        async with db.begin_nested():
                            outcome = await (_row_candidate(db, row, basis) if resource == "candidates" else _row_application(db, row))
                        counts["created" if outcome == "created" else "duplicates"] += 1
                        if outcome == "duplicate" and resource == "candidates":
                            counts["created"] += 1  # created, with a P1 duplicate suggestion
                    except Exception as exc:
                        counts["failed"] += 1
                        if len(errors) < 10_000:
                            errors.append((n, *_error_field(exc)))
    finally:
        reset_context(token)
    async with tenant_session(tenant_id) as db:
        job = await db.get(PublicImportJob, import_id)
        assert job is not None
        if errors:
            report = io.StringIO()
            writer = csv.writer(report)
            writer.writerow(["row", "field", "error"])
            writer.writerows(errors)
            job.error_report_key = storage.new_key(tenant_id)
            await storage.get_storage().put(job.error_report_key, report.getvalue().encode(), "text/csv")
        job.status, job.counts, job.completed_at = status, counts, _now()
        emit(db, PublicImportCompleted(tenant_id=tenant_id, import_id=import_id, resource=resource, status=status, **counts))
    logger.info("Public import {} {}: {}", import_id, status, counts)
    return counts


__all__ = ["create_import", "error_report", "find_by_batch", "get_import", "job_out", "parse_rows", "process_import"]

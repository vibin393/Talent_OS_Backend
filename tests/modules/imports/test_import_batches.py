"""FR-185 / NFR-25 import batches: the same file is never applied twice (Epic 13, story 13.8)."""

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, ValidationFailed
from app.services import imports_service
from tests.modules.identity.conftest import make_tenant


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def test_preview_apply_and_duplicate_refused() -> None:
    tenant_id = (await make_tenant()).id
    ctx = RequestContext(tenant_id=tenant_id)
    digest = _sha(uuid.uuid4().hex)
    async with tenant_session(tenant_id) as db:
        first = await imports_service.register_preview(
            ctx, db, "timesheets", None, digest, 10, 0, {"rows": 10}
        )
        assert first.status == "previewed"
        applied = await imports_service.mark_applied(ctx, db, first.id)
        assert applied.status == "applied" and applied.applied_at is not None
        second = await imports_service.register_preview(
            ctx, db, "timesheets", None, digest.upper(), 10, 0, {}
        )
        with pytest.raises(ConflictError) as exc:
            await imports_service.mark_applied(ctx, db, second.id)
        assert exc.value.code == "import_already_applied"
        assert exc.value.details == {"batch_id": str(first.id)}
        # Another kind with the same file is independent.
        other = await imports_service.register_preview(ctx, db, "expenses", None, digest, 1, 0, {})
        assert (await imports_service.mark_applied(ctx, db, other.id)).status == "applied"
        # Forced re-apply is allowed and audited.
        forced = await imports_service.mark_applied(ctx, db, second.id, force=True)
        assert forced.status == "applied"
        await db.flush()
        rows = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id, AuditLog.action == "import.forced_reapply"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1 and rows[0].data["previous_batch_id"] == str(first.id)


async def test_state_rules() -> None:
    tenant_id = (await make_tenant()).id
    ctx = RequestContext(tenant_id=tenant_id)
    async with tenant_session(tenant_id) as db:
        with pytest.raises(ValidationFailed):
            await imports_service.register_preview(ctx, db, "timesheets", None, "nothex", 1, 0, {})
        a = await imports_service.register_preview(ctx, db, "timesheets", None, _sha("a"), 3, 1, {})
        failed = await imports_service.mark_failed(ctx, db, a.id, reason="bad rows")
        assert failed.status == "failed" and failed.summary["failure"] == "bad rows"
        with pytest.raises(ConflictError):
            await imports_service.mark_applied(ctx, db, a.id)
        b = await imports_service.register_preview(ctx, db, "timesheets", None, _sha("b"), 3, 0, {})
        assert (await imports_service.discard(ctx, db, b.id)).status == "discarded"
        c = await imports_service.register_preview(ctx, db, "timesheets", None, _sha("c"), 3, 0, {})
        await imports_service.mark_applied(ctx, db, c.id)
        with pytest.raises(ConflictError):
            await imports_service.discard(ctx, db, c.id)

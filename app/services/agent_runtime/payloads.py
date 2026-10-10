# ruff: noqa: E501, D401
"""Encrypted payload store (Epic 53, story 53.6, NFR-47). Full prompts, model outputs and tool payloads live only here,
encrypted with the tenant's ``agent_payload`` key; logs and log columns hold redacted summaries.

Ordering inside a run is ``(step_no, seq)``: ``kickoff`` (step 1, seq 0), the pending ``request_delta`` of a step (seq 1,
written by the previous step's tx B or by ``resume``), an optional warning delta (seq 2, written with the step), the
step's ``response`` (seq 3). Tool ``tool_args`` / ``tool_result`` rows (seq 10+) are for governance reads only. History
replay never mutates earlier items (AD-57 append-only).
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encrypted import tenant_fernet
from app.core.settings import get_settings
from app.models.agent_runtime import AgentPayload, AgentRun

SEQ_KICKOFF, SEQ_DELTA, SEQ_WARNING, SEQ_RESPONSE, SEQ_TOOL = 0, 1, 2, 3, 10
HISTORY_KINDS = ("kickoff", "request_delta", "response")


def _encode(content: Any) -> bytes:
    return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_of(content: Any) -> str:
    return hashlib.sha256(_encode(content)).hexdigest()


def store(
    db: AsyncSession,
    run: AgentRun,
    kind: str,
    content: Any,
    *,
    step_no: int,
    seq: int,
    step_id: uuid.UUID | None = None,
    tool_call_id: uuid.UUID | None = None,
) -> AgentPayload:
    raw = _encode(content)
    row = AgentPayload(
        id=uuid.uuid4(),
        tenant_id=run.tenant_id,
        run_id=run.id,
        step_id=step_id,
        tool_call_id=tool_call_id,
        step_no=step_no,
        seq=seq,
        kind=kind,
        ciphertext=tenant_fernet(run.tenant_id, "agent_payload").encrypt(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        expires_at=datetime.now(UTC) + timedelta(days=get_settings().agent_payload_retention_days),
    )
    db.add(row)
    return row


def decrypt(row: AgentPayload) -> Any:
    raw = tenant_fernet(row.tenant_id, "agent_payload").decrypt(bytes(row.ciphertext))
    return json.loads(raw.decode("utf-8"))


async def rows_for(
    db: AsyncSession, run_id: uuid.UUID, kinds: tuple[str, ...] | None = None
) -> list[AgentPayload]:
    stmt = select(AgentPayload).where(AgentPayload.run_id == run_id)
    if kinds is not None:
        stmt = stmt.where(AgentPayload.kind.in_(kinds))
    stmt = stmt.order_by(AgentPayload.step_no, AgentPayload.seq, AgentPayload.created_at, AgentPayload.id)
    return list((await db.execute(stmt)).scalars())


async def history(db: AsyncSession, run_id: uuid.UUID, *, before_step: int) -> list[dict[str, Any]]:
    """The message history the model sees at step ``before_step`` (its pending delta included)."""
    messages: list[dict[str, Any]] = []
    for row in await rows_for(db, run_id, HISTORY_KINDS):
        if row.step_no > before_step:
            continue
        content = decrypt(row)
        if row.kind == "response":
            if row.step_no < before_step:
                messages.append({"role": "assistant", "content": content})
        else:
            messages.extend(content)
    return messages


async def purge_run(db: AsyncSession, run_id: uuid.UUID) -> int:
    result = await db.execute(delete(AgentPayload).where(AgentPayload.run_id == run_id))
    return int(getattr(result, "rowcount", 0) or 0)


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    result = await db.execute(
        delete(AgentPayload).where(AgentPayload.expires_at < (now or datetime.now(UTC)))
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "SEQ_DELTA",
    "SEQ_KICKOFF",
    "SEQ_RESPONSE",
    "SEQ_TOOL",
    "SEQ_WARNING",
    "decrypt",
    "history",
    "purge_expired",
    "purge_run",
    "rows_for",
    "sha256_of",
    "store",
]

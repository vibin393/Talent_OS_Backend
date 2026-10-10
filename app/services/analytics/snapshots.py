# ruff: noqa: E501, D401
"""Training-set snapshots (Epic 48 A3 / A8).

The training set (subject id, ``candidate_id`` where applicable, ``as_of``, outcome date, features, label) is
written as Parquet through pyarrow (CSV fallback) to ``core.storage`` under the tenant prefix
(``core.storage.new_key(tenant_id)`` = ``t/<tenant>/<yyyy>/<mm>/<hex>``; the storage port only accepts that key
shape). Features are one JSON column (sorted keys; Python's float ``repr`` round-trips exactly), so a model can be
refitted byte-identically from the stored snapshot plus its seed.

``expires_at = built_at + 90 days``; ``purge_expired`` (beat, queue ``maintenance``) deletes expired files and rows.
Erasure rewrites a snapshot without the subject's rows (``rewrite_without``).
"""

import csv
import io
import json
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.models.analytics import MlTrainingSet
from app.services.analytics.features.common import TrainingRow

TTL = timedelta(days=90)
COLUMNS = ("subject_id", "candidate_id", "as_of", "outcome_at", "label", "features")


def _record(r: TrainingRow) -> dict[str, object]:
    return {
        "subject_id": str(r.subject_id),
        "candidate_id": str(r.candidate_id) if r.candidate_id else None,
        "as_of": r.as_of.isoformat(),
        "outcome_at": r.outcome_at.isoformat(),
        "label": float(r.label),
        "features": json.dumps(r.features, sort_keys=True, default=str),
    }


def _row(rec: dict[str, object]) -> TrainingRow:
    cand = rec.get("candidate_id")
    return TrainingRow(
        subject_id=uuid.UUID(str(rec["subject_id"])),
        candidate_id=uuid.UUID(str(cand)) if cand not in (None, "") else None,
        as_of=datetime.fromisoformat(str(rec["as_of"])),
        outcome_at=datetime.fromisoformat(str(rec["outcome_at"])),
        features=json.loads(str(rec["features"])),
        label=float(rec["label"]),  # type: ignore[arg-type]
    )


def encode(rows: Sequence[TrainingRow]) -> tuple[bytes, str]:
    records = [_record(r) for r in rows]
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover - pyarrow is pinned; CSV is the documented fallback
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(COLUMNS))
        writer.writeheader()
        writer.writerows(
            {k: ("" if v is None else repr(v) if k == "label" else v) for k, v in rec.items()}
            for rec in records
        )
        return buf.getvalue().encode("utf-8"), "csv"
    table = pa.table(
        {
            "subject_id": pa.array([r["subject_id"] for r in records], pa.string()),
            "candidate_id": pa.array([r["candidate_id"] for r in records], pa.string()),
            "as_of": pa.array([r["as_of"] for r in records], pa.string()),
            "outcome_at": pa.array([r["outcome_at"] for r in records], pa.string()),
            "label": pa.array([r["label"] for r in records], pa.float64()),
            "features": pa.array([r["features"] for r in records], pa.string()),
        }
    )
    out = io.BytesIO()
    pq.write_table(table, out)
    return out.getvalue(), "parquet"


def decode(data: bytes, fmt: str) -> list[TrainingRow]:
    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
        return [_row({**rec, "label": float(rec["label"])}) for rec in reader]
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(data))
    return [_row(rec) for rec in table.to_pylist()]


def roundtrip(rows: Sequence[TrainingRow]) -> list[TrainingRow]:
    """The rows exactly as a stored snapshot will return them (training fits on these)."""
    data, fmt = encode(rows)
    return decode(data, fmt)


async def write(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    spec_version: int,
    rows: Sequence[TrainingRow],
    *,
    n_minority: int | None,
    holdout_from: date | None,
    now: datetime | None = None,
) -> MlTrainingSet:
    built = now or datetime.now(UTC)
    data, fmt = encode(rows)
    key = storage.new_key(tenant_id, built)
    await storage.get_storage().put(
        key, data, "application/vnd.apache.parquet" if fmt == "parquet" else "text/csv"
    )
    ts = MlTrainingSet(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        model_kind=kind,
        spec_version=spec_version,
        built_at=built,
        row_count=len(rows),
        n_minority=n_minority,
        storage_key=key,
        format=fmt,
        holdout_from=holdout_from,
        expires_at=built + TTL,
        candidate_ids=sorted({r.candidate_id for r in rows if r.candidate_id is not None}, key=str),
    )
    db.add(ts)
    await db.flush()
    return ts


async def read(ts: MlTrainingSet) -> list[TrainingRow]:
    return decode(await storage.get_storage().get(ts.storage_key), ts.format)


async def rewrite_without(db: AsyncSession, ts: MlTrainingSet, candidate_ids: Iterable[uuid.UUID]) -> int:
    """Rewrite the snapshot without the rows of ``candidate_ids``; returns the removed row count."""
    drop = set(candidate_ids)
    rows = await read(ts)
    kept = [r for r in rows if r.candidate_id not in drop]
    data, fmt = encode(kept)
    await storage.get_storage().put(
        ts.storage_key, data, "application/vnd.apache.parquet" if fmt == "parquet" else "text/csv"
    )
    ts.format = fmt
    ts.row_count = len(kept)
    ts.candidate_ids = [c for c in ts.candidate_ids if c not in drop]
    ts.rewritten_for_erasure_at = datetime.now(UTC)
    await db.flush()
    return len(rows) - len(kept)


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    from app.models.analytics import MlModel

    cutoff = now or datetime.now(UTC)
    expired = list(
        (await db.execute(select(MlTrainingSet).where(MlTrainingSet.expires_at <= cutoff))).scalars()
    )
    for ts in expired:
        try:
            await storage.get_storage().delete(ts.storage_key)
        except Exception:  # best effort; the row goes anyway
            logger.warning("analytics: could not delete snapshot {}", ts.id)
        for model in (await db.execute(select(MlModel).where(MlModel.training_set_id == ts.id))).scalars():
            model.training_set_id = None
        await db.delete(ts)
    await db.flush()
    return len(expired)

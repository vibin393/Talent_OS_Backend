# ruff: noqa: E501
"""Job board settings, default board sets and taxonomy mappings (Epic 70, spec A3 / A7)."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.integrations import find_manifest
from app.models.job_distribution import BoardFieldMapping, BoardSet
from app.schemas.job_distribution import (
    BoardFieldOut,
    BoardSetIn,
    BoardSetOut,
    BoardSetPatch,
    FieldOptionOut,
    JobBoardSettingsIn,
    JobBoardSettingsOut,
    MappingOut,
    MappingsPut,
)
from app.services.job_distribution.common import board_settings, tenant_of


async def get_settings_out(db: AsyncSession) -> JobBoardSettingsOut:
    return JobBoardSettingsOut.model_validate(await board_settings(db))


async def put_settings(db: AsyncSession, data: JobBoardSettingsIn) -> JobBoardSettingsOut:
    row = await board_settings(db)
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "default_board_set_id" and value is not None and await db.get(BoardSet, value) is None:
            raise NotFound("Board set not found", code="board_set_not_found")
        if value is not None or field == "default_board_set_id":
            setattr(row, field, value)
    await db.flush()
    return JobBoardSettingsOut.model_validate(row)


def _check_keys(keys: list[str]) -> list[str]:
    bad = [k for k in keys if (m := find_manifest(k)) is None or m.category != "job_board"]
    if bad:
        raise ValidationFailed("Unknown job board", code="unknown_board", details={"fields": {"board_keys": ", ".join(bad)}})
    return list(dict.fromkeys(keys))


async def _clear_default(db: AsyncSession, employment_type: str | None, keep: uuid.UUID) -> None:
    for s in (await db.execute(select(BoardSet).where(BoardSet.is_default.is_(True), BoardSet.id != keep))).scalars():
        if s.employment_type == employment_type:
            s.is_default = False
    await db.flush()


async def list_sets(db: AsyncSession) -> list[BoardSetOut]:
    rows = (await db.execute(select(BoardSet).order_by(BoardSet.employment_type.nulls_last(), BoardSet.name))).scalars()
    return [BoardSetOut.model_validate(r) for r in rows]


async def create_set(db: AsyncSession, data: BoardSetIn) -> BoardSetOut:
    row = BoardSet(id=uuid.uuid4(), tenant_id=tenant_of(db), name=data.name, employment_type=data.employment_type, board_keys=_check_keys(data.board_keys), is_default=False)
    db.add(row)
    await db.flush()
    if data.is_default:
        await _clear_default(db, row.employment_type, row.id)
        row.is_default = True
        await db.flush()
    return BoardSetOut.model_validate(row)


async def update_set(db: AsyncSession, set_id: uuid.UUID, data: BoardSetPatch) -> BoardSetOut:
    row = await db.get(BoardSet, set_id)
    if row is None:
        raise NotFound("Board set not found", code="board_set_not_found")
    if data.name is not None:
        row.name = data.name
    if data.board_keys is not None:
        row.board_keys = _check_keys(data.board_keys)
    if data.is_default is not None:
        if data.is_default:
            await _clear_default(db, row.employment_type, row.id)
        row.is_default = data.is_default
    await db.flush()
    return BoardSetOut.model_validate(row)


async def delete_set(db: AsyncSession, set_id: uuid.UUID) -> None:
    row = await db.get(BoardSet, set_id)
    if row is None:
        raise NotFound("Board set not found", code="board_set_not_found")
    await db.delete(row)
    await db.flush()


async def list_mappings(db: AsyncSession, board_key: str) -> list[MappingOut]:
    _check_keys([board_key])
    rows = (await db.execute(select(BoardFieldMapping).where(BoardFieldMapping.board_key == board_key).order_by(BoardFieldMapping.kind, BoardFieldMapping.local_value))).scalars()
    return [MappingOut.model_validate(r) for r in rows]


async def put_mappings(db: AsyncSession, board_key: str, data: MappingsPut) -> list[MappingOut]:
    """Replace the board's mappings."""
    _check_keys([board_key])
    seen: set[tuple[str, str]] = set()
    for item in data.items:
        k = (item.kind, item.local_value.lower())
        if k in seen:
            raise ValidationFailed("Each Talent OS value can be mapped once per kind", code="duplicate_mapping", details={"fields": {"items": f"{item.kind}: {item.local_value}"}})
        seen.add(k)
    await db.execute(delete(BoardFieldMapping).where(BoardFieldMapping.board_key == board_key))
    tid = tenant_of(db)
    for item in data.items:
        db.add(BoardFieldMapping(id=uuid.uuid4(), tenant_id=tid, board_key=board_key, kind=item.kind, local_value=item.local_value, board_value=item.board_value, board_label=item.board_label))
    try:
        await db.flush()
    except IntegrityError:
        raise ConflictError("Duplicate mapping", code="duplicate_mapping") from None
    return await list_mappings(db, board_key)


def schema_out(schema: object) -> list[BoardFieldOut]:
    return [
        BoardFieldOut(key=f.key, label=f.label, required=f.required, kind=f.kind, options=[FieldOptionOut(value=v, label=lbl) for v, lbl in f.options])
        for f in getattr(schema, "fields", ())
    ]

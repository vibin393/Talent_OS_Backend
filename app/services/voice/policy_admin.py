# ruff: noqa: E501
"""Recording Policy settings (Epic 58, story 58.2; FR-294) on the foundation table ``recording_policies``.

``list_rules``, ``upsert_rule`` (country + optional state/region; the India seed can be edited or deleted),
``delete_rule`` and ``evaluate(country, region)`` (the UI's "Test a location": region row, else country row, else not
permitted). Changes are audited with before / after. The tenant and its counsel own these rules; Talent OS enforces
consent and the rule set here through ``recording_policy.gate.check``.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.recording_policy import RecordingPolicy
from app.schemas.voice import PolicyEvaluationOut, RecordingPolicyIn, RecordingPolicyOut
from app.services.voice import common


async def _out(db: AsyncSession, row: RecordingPolicy) -> RecordingPolicyOut:
    from app.services import entity_refs

    who = await entity_refs.user(db, row.updated_by) if row.updated_by else None
    return RecordingPolicyOut(
        id=row.id, country=row.country, region=row.region, rule=row.rule, note=row.note, updated_by=row.updated_by,  # type: ignore[arg-type]
        updated_by_name=who.name if who is not None else None, updated_at=row.updated_at,
    )  # fmt: skip


async def list_rules(ctx: RequestContext, db: AsyncSession) -> list[RecordingPolicyOut]:  # noqa: ARG001
    rows = (
        (
            await db.execute(
                select(RecordingPolicy).order_by(
                    RecordingPolicy.country, RecordingPolicy.region.nulls_first()
                )
            )
        )
        .scalars()
        .all()
    )
    return [await _out(db, r) for r in rows]


def _normalise(data: RecordingPolicyIn) -> tuple[str, str | None]:
    from app.services.recording_policy.gate import normalize_country, normalize_region

    country = normalize_country(data.country)
    if country is None:
        raise common.invalid(
            "invalid_country", "Use a two-letter country code", fields={"country": "Unknown country"}
        )
    return country, normalize_region(data.region)


async def _find(db: AsyncSession, country: str, region: str | None) -> RecordingPolicy | None:
    stmt = select(RecordingPolicy).where(RecordingPolicy.country == country)
    stmt = (
        stmt.where(RecordingPolicy.region.is_(None))
        if region is None
        else stmt.where(RecordingPolicy.region == region)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def upsert_rule(
    ctx: RequestContext, db: AsyncSession, data: RecordingPolicyIn, rule_id: uuid.UUID | None = None
) -> RecordingPolicyOut:
    from app.services import entity_refs

    country, region = _normalise(data)
    row = await db.get(RecordingPolicy, rule_id) if rule_id else await _find(db, country, region)
    if rule_id and row is None:
        raise NotFound("Rule not found")
    clash = await _find(db, country, region)
    if clash is not None and row is not None and clash.id != row.id:
        raise common.conflict("jurisdiction_exists", "There is already a rule for this location")
    before = (
        None
        if row is None
        else {"country": row.country, "region": row.region, "rule": row.rule, "note": row.note}
    )
    if row is None:
        row = RecordingPolicy(
            id=uuid.uuid4(),
            tenant_id=entity_refs.session_tenant(db),
            country=country,
            region=region,
            rule=data.rule,
        )
        db.add(row)
    row.country, row.region, row.rule, row.note = country, region, data.rule, data.note
    row.updated_by = common.user_id_of(ctx)
    await db.flush()
    await db.refresh(row)
    audit.record(
        "recording_policy.changed", "recording_policy", row.id, session=db, before=before,
        after={"country": row.country, "region": row.region, "rule": row.rule, "note": row.note},
    )  # fmt: skip
    return await _out(db, row)


async def delete_rule(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID) -> None:  # noqa: ARG001
    row = await db.get(RecordingPolicy, rule_id)
    if row is None:
        raise NotFound("Rule not found")
    before = {"country": row.country, "region": row.region, "rule": row.rule, "note": row.note}
    await db.delete(row)
    await db.flush()
    audit.record(
        "recording_policy.deleted", "recording_policy", rule_id, session=db, before=before, after=None
    )


async def evaluate(
    ctx: RequestContext, db: AsyncSession, country: str | None, region: str | None  # noqa: ARG001
) -> PolicyEvaluationOut:
    from app.services.recording_policy.gate import normalize_country, normalize_region

    code = normalize_country(country)
    reg = normalize_region(region)
    if code is None:
        return PolicyEvaluationOut(
            country=None, region=reg, jurisdiction=None, rule="not_permitted", matched="none"
        )
    label = f"{code}-{reg}" if reg else code
    if reg is not None:
        row = await _find(db, code, reg)
        if row is not None:
            return PolicyEvaluationOut(
                country=code, region=reg, jurisdiction=label, rule=row.rule, matched="region"
            )  # type: ignore[arg-type]
    row = await _find(db, code, None)
    if row is not None:
        return PolicyEvaluationOut(
            country=code, region=reg, jurisdiction=label, rule=row.rule, matched="country"
        )  # type: ignore[arg-type]
    return PolicyEvaluationOut(
        country=code, region=reg, jurisdiction=label, rule="not_permitted", matched="none"
    )


__all__ = ["delete_rule", "evaluate", "list_rules", "upsert_rule"]

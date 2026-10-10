# ruff: noqa: E501
"""Workflow signals of predictions (Epic 48 A6, AD-45): notify-only.

* The trigger ``analytics.prediction_band_changed`` is registered by the foundation catalogue (entity
  ``prediction``, attr ``prediction_id``). This module registers the ``prediction`` snapshot loader and restricts
  the trigger's actions to exactly ``{notify, create_task}`` ("escalate" = ``create_task`` with its
  ``escalation_policy_id``). Every other action -- ``send_email``, ``comms.*``, ``move_stage``, ``add_tag``,
  ``assign``, ``refresh_matches``, ... -- is refused at save and at run (422 ``action_not_allowed_for_trigger``).
* ``prediction`` is not an actor: rules run as ``workflow``, which is already refused every human-only transition.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext

TRIGGER_KEY = "analytics.prediction_band_changed"
ALLOWED_ACTIONS = frozenset({"notify", "create_task"})


async def owner_of_subject(
    db: AsyncSession, subject_kind: str, subject_id: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """(owner user id, demand id) of a prediction subject: Demand owner, Offer owner, Placement account manager."""
    from sqlalchemy import select

    if subject_kind == "demand":
        from app.models.demands import Demand

        owner = (
            await db.execute(select(Demand.owner_id).where(Demand.id == subject_id))
        ).scalar_one_or_none()
        return owner, subject_id
    if subject_kind == "offer":
        from app.models.offers import Offer

        row = (
            await db.execute(select(Offer.owner_id, Offer.demand_id).where(Offer.id == subject_id))
        ).first()
        return (row[0], row[1]) if row else (None, None)
    from app.models.placements import Placement

    row = (
        await db.execute(
            select(Placement.account_manager_user_id, Placement.demand_id).where(Placement.id == subject_id)
        )
    ).first()
    return (row[0], row[1]) if row else (None, None)


async def snapshot(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID) -> dict[str, Any] | None:  # noqa: ARG001
    from app.models.analytics import MlPrediction

    p = await db.get(MlPrediction, entity_id)
    if p is None:
        return None
    owner, demand_id = await owner_of_subject(db, p.subject_kind, p.subject_id)
    return {
        "model_kind": p.kind,
        "subject_kind": p.subject_kind,
        "subject_id": str(p.subject_id),
        "band": p.band,
        "score": str(p.score),
        "owner_id": str(owner) if owner else None,
        "demand_id": str(demand_id) if demand_id else None,
        f"{p.subject_kind}_id": str(p.subject_id),
    }


def register() -> None:
    from app.models.analytics import BANDS, MODEL_KINDS, SUBJECT_KINDS
    from app.services.workflow import catalogue
    from app.services.workflow.conditions import FieldDef
    from app.services.workflow.registry import restrict_trigger_actions

    restrict_trigger_actions(TRIGGER_KEY, ALLOWED_ACTIONS)
    if catalogue.snapshot_loader("prediction") is None:
        catalogue.register_snapshot_loader(
            "prediction",
            snapshot,
            {
                "model_kind": FieldDef("enum", "Model", MODEL_KINDS),
                "subject_kind": FieldDef("enum", "Subject", SUBJECT_KINDS),
                "band": FieldDef("enum", "Band", BANDS),
                "score": FieldDef("decimal", "Score"),
            },
        )

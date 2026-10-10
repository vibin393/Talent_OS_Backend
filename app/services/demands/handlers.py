"""Logging handlers for demand events (Epic 10 replaces them with notifications). Never log PII."""

from loguru import logger

from app.core.events import subscribe
from app.events.demands import (
    DemandAssigned,
    DemandCreated,
    DemandOpened,
    DemandPublished,
    DemandSlaStatusChanged,
    DemandStatusChanged,
    DemandSubmitted,
    DemandUnpublished,
)


def _log_created(e: DemandCreated) -> None:
    logger.info("Demand created: demand={} actor={}", e.demand_id, e.actor_kind)


def _log_submitted(e: DemandSubmitted) -> None:
    logger.info(
        "Demand submitted: demand={} auto_approved={} request={}",
        e.demand_id,
        e.auto_approved,
        e.approval_request_id,
    )


def _log_opened(e: DemandOpened) -> None:
    logger.info("Demand opened: demand={}", e.demand_id)


def _log_status(e: DemandStatusChanged) -> None:
    logger.info(
        "Demand status: demand={} {} -> {} actor={} reason={}",
        e.demand_id,
        e.from_status,
        e.to_status,
        e.actor_kind,
        e.reason_code,
    )


def _log_assigned(e: DemandAssigned) -> None:
    logger.info(
        "Demand assigned: demand={} added_users={} team_members={} primary={}",
        e.demand_id,
        len(e.added_user_ids),
        len(e.expanded_team_member_ids),
        e.primary_user_id,
    )


def _log_published(e: DemandPublished) -> None:
    logger.info("Demand published: demand={} jd_version={}", e.demand_id, e.jd_version_id)


def _log_unpublished(e: DemandUnpublished) -> None:
    logger.info("Demand unpublished: demand={} actor={}", e.demand_id, e.actor_kind)


def _log_sla(e: DemandSlaStatusChanged) -> None:
    logger.info(
        "Demand SLA status: demand={} milestone={} status={} recipients={}",
        e.demand_id,
        e.milestone,
        e.status,
        len(e.recipient_ids),
    )


def register() -> None:
    subscribe(DemandCreated, _log_created)
    subscribe(DemandSubmitted, _log_submitted)
    subscribe(DemandOpened, _log_opened)
    subscribe(DemandStatusChanged, _log_status)
    subscribe(DemandAssigned, _log_assigned)
    subscribe(DemandPublished, _log_published)
    subscribe(DemandUnpublished, _log_unpublished)
    subscribe(DemandSlaStatusChanged, _log_sla)

# ruff: file-ignore[line-too-long]
"""Public API v1 routers (Epic 66 conventions, AD-77). Mounted at ``/api/public/v1`` (served as ``/public/v1``).
Every router is built with ``core.public_api.public_router(<resource>)``; Epic 67 adds the resources (FR-346) and
Epic 68 ``webhooks`` / ``events``: append them to ``get_public_v1_routers`` (additive)."""

from fastapi import APIRouter

from app.core.public_api import PUBLIC_PREFIX


def get_public_v1_routers() -> list[APIRouter]:
    from app.router.public_v1 import meta

    return [meta.router, *_epic67_routers(), *_epic68_routers(), *_unified_ingest_routers()]


def _unified_ingest_routers() -> list[APIRouter]:
    """Unified-platform hub ingest: Croar assessments and Feedback IQ engagement signals (additive)."""
    from app.router.public_v1 import ingest

    return [ingest.assessments_router, ingest.engagement_router]


def _epic68_routers() -> list[APIRouter]:
    """Epic 68 (FR-349..FR-353): an App's own ``/webhooks``, the ``/event_types`` catalogue and ``/events``."""
    from app.router.public_v1 import events, webhooks

    return [webhooks.router, events.router]


def _epic67_routers() -> list[APIRouter]:
    """Epic 67 (FR-346): the v1 resources and the public changelog."""
    from app.router.public_v1 import (
        activities,
        applications,
        candidates,
        changelog,
        clients,
        contacts,
        credit_notes,
        demands,
        expenses,
        imports,
        interviews,
        invoices,
        notes,
        offers,
        payments,
        placements,
        pools,
        postings,
        skills,
        submissions,
        tasks,
        timesheets,
        users,
        workers,
    )

    return [
        candidates.router, applications.router, demands.router, postings.router, notes.router, tasks.router,
        activities.router, clients.router, contacts.router, users.router, pools.router, skills.router,
        submissions.router, interviews.router, offers.router, placements.router, workers.router, timesheets.router,
        expenses.router, invoices.router, credit_notes.router, payments.router, imports.router, changelog.router,
    ]  # fmt: skip


def build_public_api_router() -> APIRouter:
    api = APIRouter(prefix=PUBLIC_PREFIX)
    for router in get_public_v1_routers():
        api.include_router(router)
    return api

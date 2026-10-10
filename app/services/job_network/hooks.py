# ruff: noqa: E501
"""Job Network wiring (Epic 75), autodiscovered by ``core.hooks``.

* the outbox sink ``job_network`` (index sync over ``posting.published|closed|updated`` and ``demand.status_changed``);
* the internal Public Event ``posting.updated`` (AD-87) over the demand-publish event, if no epic registered it first,
  and the network's own ``network_listing.listed|unlisted`` Public Events;
* the ``job_network`` careers apply-source resolver (D-18) and the ``job_network_application`` default notice;
* notification types, the settings-hub entry and audit action labels.
"""

import uuid

from loguru import logger
from pydantic import BaseModel, ConfigDict


def _sink() -> None:
    from app.services import public_events_service
    from app.services.job_network import sink

    if not any(s.name == "job_network" for s in public_events_service.sinks()):
        public_events_service.register_sink(
            "job_network", sink.on_rows, event_types=sink.EVENT_TYPES, include_internal=True
        )


class _PostingUpdatedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    demand_id: uuid.UUID
    posting_id: uuid.UUID | None = None
    changed_fields: list[str] = []


class _NetworkListingData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    demand_id: uuid.UUID
    posting_id: uuid.UUID | None = None
    listing_id: uuid.UUID
    status: str


def _public_events() -> None:
    from app.core.public_events import PublicEventSpec, all_specs, register_public_event
    from app.events.demands import DemandPublished
    from app.events.job_network import NetworkListingChanged

    known = {s.type for s in all_specs(include_internal=True)}

    if "posting.updated" not in known:
        register_public_event(
            PublicEventSpec(
                type="posting.updated", version=1, source=DemandPublished, subject_type="posting",
                subject_id=lambda e: e.demand_id,
                data_model=_PostingUpdatedData,
                build=lambda e: _PostingUpdatedData(demand_id=e.demand_id, changed_fields=["published"]),
                scopes=("postings:read",), visibility="internal", owner_epic="75",
                description="A demand posting's careers-public content changed (internal connector trigger).",
            )
        )

    def listed_build(e: NetworkListingChanged) -> _NetworkListingData | None:
        if e.status not in ("listed",):
            return None
        return _NetworkListingData(demand_id=e.demand_id, listing_id=e.listing_id, status=e.status)

    def unlisted_build(e: NetworkListingChanged) -> _NetworkListingData | None:
        if e.status not in ("unlisted", "suspended", "removed", "held"):
            return None
        return _NetworkListingData(demand_id=e.demand_id, listing_id=e.listing_id, status=e.status)

    for type_, build in (("network_listing.listed", listed_build), ("network_listing.unlisted", unlisted_build)):
        if type_ in known:
            continue
        register_public_event(
            PublicEventSpec(
                type=type_, version=1, source=NetworkListingChanged, subject_type="network_listing",
                subject_id=lambda e: e.listing_id, data_model=_NetworkListingData, build=build,
                scopes=("postings:read",), owner_epic="75",
                description="A job network listing was listed / unlisted (no applicant data).",
            )
        )


def _apply_resolver() -> None:
    from app.services.job_network import apply_resolver
    from app.services.portal import apply

    apply.register_apply_source_resolver("job_network", apply_resolver.resolve)


def _notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice(
        "job_network_application",
        "Talent OS Jobs application",
        "You found this job on Talent OS Jobs. Your application goes only to {agency}. Talent OS Jobs does not keep a "
        "copy and does not share it with other agencies.",
    )


def _settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting", "job_network", "Job network",
        "List open jobs on Talent OS Jobs; applications still arrive on your careers site.",
        "/api/v1/job-network/settings", "network:manage", owner_epic="Epic 75",
    )


def _audit() -> None:
    import contextlib

    from app.core import audit

    for key, text in (
        ("job_network.verify", "A job network agency was verified"),
        ("job_network.reject_verification", "A job network agency's verification was rejected"),
        ("job_network.suspend", "A job network listing / agency was suspended"),
        ("job_network.reinstate", "A job network listing / agency was reinstated"),
        ("job_network.remove", "A job network listing was removed"),
        ("job_network.release_hold", "A job network listing hold was released"),
        ("job_network.uphold_report", "A job network report was upheld"),
        ("job_network.dismiss_report", "A job network report was dismissed"),
    ):
        with contextlib.suppress(Exception):  # duplicate registration is harmless
            audit.register_audit_action(key, text)


for _fn in (_sink, _public_events, _apply_resolver, _notice, _settings, _audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("job network hooks: {} failed", _fn.__name__)
        raise

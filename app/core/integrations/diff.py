"""Manifest diff for re-consent (Epic 66 ships it; Epic 69 uses it): what a new manifest version adds."""

import dataclasses

from app.core.integrations.manifest import ConnectorManifest, DataFlow

__all__ = ["ManifestDiff", "manifest_diff"]


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestDiff:
    added_scopes: tuple[str, ...]  # OAuth scopes + capabilities newly requested
    added_flows: tuple[DataFlow, ...]
    added_hosts: tuple[str, ...]
    requires_reconsent: bool


def _scopes(m: ConnectorManifest) -> set[str]:
    return set(m.oauth.scopes if m.oauth else ()) | {f"capability:{c}" for c in m.capabilities}


def _flow_key(f: DataFlow) -> tuple[object, ...]:
    return (f.direction, f.entity, f.purpose, f.destination_country)


def _flow_widens(new: DataFlow, old: DataFlow | None) -> bool:
    if old is None:
        return True
    return bool(set(new.fields) - set(old.fields)) or (new.personal_data and not old.personal_data)


def manifest_diff(old: ConnectorManifest, new: ConnectorManifest) -> ManifestDiff:
    """Additions from ``old`` to ``new``. Re-consent is required when scopes / capabilities, data flows (new flow,
    new fields, newly personal) or egress hosts are added, or the auth type changes. Removals never require it."""
    added_scopes = tuple(sorted(_scopes(new) - _scopes(old)))
    old_flows = {_flow_key(f): f for f in old.data_flows}
    added_flows = tuple(f for f in new.data_flows if _flow_widens(f, old_flows.get(_flow_key(f))))
    added_hosts = tuple(sorted(set(new.egress_hosts) - set(old.egress_hosts)))
    requires = bool(added_scopes or added_flows or added_hosts) or new.auth != old.auth
    return ManifestDiff(added_scopes, added_flows, added_hosts, requires)

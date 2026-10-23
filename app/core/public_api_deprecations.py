# ruff: file-ignore[line-too-long]
"""Public API v1 deprecations (Epic 67, FR-347, spec A5): ``route template -> since, sunset, replacement``.

Routes apply ``core.public_api.deprecate(**DEPRECATIONS[route])``; the request log marks the call ``deprecated`` and the
monthly report (``public_api.deprecation_report``) notifies the integrations still calling it. A sunset is at least 12
months after ``since`` (``validate``). Empty in v1.0: nothing is deprecated yet.
"""

import dataclasses
from datetime import date


@dataclasses.dataclass(frozen=True, slots=True)
class Deprecation:
    since: date
    sunset: date
    replacement: str  # a route template or a docs link


DEPRECATIONS: dict[str, Deprecation] = {}


def validate(entries: dict[str, Deprecation] | None = None) -> list[str]:
    problems = []
    for route, dep in (entries if entries is not None else DEPRECATIONS).items():
        months = (dep.sunset.year - dep.since.year) * 12 + dep.sunset.month - dep.since.month
        if months < 12 or (months == 12 and dep.sunset.day < dep.since.day):
            problems.append(f"{route}: sunset must be at least 12 months after since")
    return problems


__all__ = ["DEPRECATIONS", "Deprecation", "validate"]

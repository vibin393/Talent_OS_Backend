# ruff: noqa: E501
"""Named seed builders for eval cases. A builder receives the test org (tenant, owner, candidate) and returns the
placeholder values its script may use. Domain epics add builders (e.g. ``demand_with_applications``)."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

SeedBuilder = Callable[[Any], dict[str, str]]


def candidate(org: Any) -> dict[str, str]:
    return {
        "candidate_id": str(org.candidate_id),
        "other_candidate_id": str(uuid.uuid4()),
        "owner_id": str(org.admin.user_id),
        "random_id": str(uuid.uuid4()),
    }


BUILDERS: dict[str, SeedBuilder] = {"candidate": candidate}

# Epic 56 (additive): async seed builders (they create records through the API / services) live in
# ``evals/seeds_<domain>.py`` modules, loaded on first use; a builder may return ``subject_type`` / ``subject_id``
# (case ``subject: seed``) and ``subject_candidate_ids`` (comma-separated, for the no-cross-subject check).
AsyncSeedBuilder = Callable[[Any], Awaitable[dict[str, str]]]
ASYNC_BUILDERS: dict[str, AsyncSeedBuilder] = {}


def register_async_builder(name: str, fn: AsyncSeedBuilder) -> None:
    ASYNC_BUILDERS[name] = fn


def load_seed_modules() -> None:
    import importlib
    import pkgutil
    from pathlib import Path

    for info in pkgutil.iter_modules([str(Path(__file__).resolve().parent)]):
        if info.name.startswith("seeds_"):
            importlib.import_module(f"evals.{info.name}")


async def build(name: str, org: Any) -> dict[str, str]:
    if name not in BUILDERS:
        load_seed_modules()
    if name in ASYNC_BUILDERS:
        return await ASYNC_BUILDERS[name](org)
    return BUILDERS[name](org)


def fill(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in values.items():
            value = value.replace("{{" + key + "}}", replacement)
        return value
    if isinstance(value, list):
        return [fill(v, values) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, values) for k, v in value.items()}
    return value


__all__ = ["ASYNC_BUILDERS", "BUILDERS", "AsyncSeedBuilder", "SeedBuilder", "build", "candidate", "fill", "load_seed_modules", "register_async_builder"]

# ruff: noqa: E501
"""Epic 56 eval seed ``sourcing_demand``: an open Demand with ranked matches, a search-only candidate without AI
consent and excluded candidates (pipeline, archived, do-not-contact); the Sourcing Agent enabled. Placeholders:
``{{demand_id}}``, ``{{cand_a}}`` .. ``{{cand_g}}`` (see ``tests/agents/sourcing/helpers.py``)."""

from typing import Any

from evals.fixtures import register_async_builder


async def sourcing_demand(org: Any) -> dict[str, str]:
    from tests.agents.sourcing.helpers import seed_sourcing

    seed = await seed_sourcing(org.admin, org.tenant_id)
    return seed.values()


register_async_builder("sourcing_demand", sourcing_demand)

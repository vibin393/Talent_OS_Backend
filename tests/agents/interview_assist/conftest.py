# ruff: noqa: E501
"""Epic 60 fixtures: an Interview with two internal interviewers on the Epic 53 ``agent_org`` (``helpers``)."""

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI

from tests.agents.conftest import AgentOrg
from tests.agents.interview_assist.helpers import Seed, seed_interview
from tests.modules.pipeline.helpers import outbox, public  # noqa: F401


@pytest.fixture
async def seed(app: FastAPI, agent_org: AgentOrg) -> AsyncIterator[Seed]:
    s = await seed_interview(app, agent_org.admin, agent_org.tenant, agent_org.candidate_id)
    yield s
    await s.ann.client.aclose()
    await s.bob.client.aclose()


@pytest.fixture(autouse=True)
def _no_transcriber() -> Iterator[None]:
    from app.services import interview_captures_service as svc

    svc.set_transcription_provider(None)
    yield
    svc.set_transcription_provider(None)

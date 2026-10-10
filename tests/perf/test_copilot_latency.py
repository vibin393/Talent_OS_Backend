# ruff: noqa: E501
"""Epic 54 story 54.6 (NFR-48, ``-m perf``). The model is the stateless keyless Fake LLM (model time ~0), so measured
time is platform-added. Targets: no-tool answers p95 <= 4 s, answers with up to 3 tool calls p95 <= 8 s.

* ``test_turn_latency_in_process`` -- one user, eager Celery: the whole turn (run steps, tools, answer recording,
  stream events) completes inside the POST, so the POST time bounds the first delta and the draft render.
* ``test_concurrent_users_against_workers`` -- the NFR-48 load shape (``COPILOT_PERF_USERS`` concurrent users,
  default 50) needs a real ``worker-conversations -c 8`` with ``CELERY_TASK_ALWAYS_EAGER=false``; it runs only with
  ``COPILOT_PERF_WORKERS=1`` (eager Celery serialises every step in-process, which is not the production shape).
"""

import asyncio
import os
import statistics
import time
import uuid
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI

from app.core.settings import get_settings
from app.services import ai
from app.services.ai.fake_llm import keyless_factory
from tests.agents.conftest import _agent_env, _agents_registered  # noqa: F401
from tests.agents.copilot.conftest import C, h, make_org
from tests.modules.identity.conftest import join

pytestmark = [pytest.mark.perf, pytest.mark.slow]

TOOLS = [["sla.at_risk", {}], ["tasks.list", {}], ["demands.list", {"status": ["open"]}]]


def _p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=20)[-1] if len(values) > 1 else values[0]


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_tools: bool) -> None:
    turns = ([{"kind": "tool_calls", "calls": TOOLS}] if with_tools else []) + [
        {"kind": "text", "text": "Nothing is at risk right now."}
    ]
    (tmp_path / "agent_copilot.yaml").write_text(yaml.safe_dump({"agent_copilot": turns}), encoding="utf-8")
    (tmp_path / "default.yaml").write_text(
        yaml.safe_dump([{"kind": "text", "text": "Hello."}]), encoding="utf-8"
    )
    monkeypatch.setattr(get_settings(), "ai_fake_script_dir", str(tmp_path))
    ai.set_client_factory(keyless_factory())


async def _ask(m, conv: str) -> float:  # noqa: ANN001
    start = time.perf_counter()
    resp = await m.client.post(
        f"{C}/conversations/{conv}/messages", json={"text": "Which open demands are at risk?"},
        headers=h(m, **{"Idempotency-Key": uuid.uuid4().hex}),
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    return time.perf_counter() - start


@pytest.mark.parametrize(("with_tools", "target"), [(False, 4.0), (True, 8.0)])
async def test_turn_latency_in_process(
    app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_tools: bool, target: float
) -> None:
    org = await make_org(app)
    _install(tmp_path, monkeypatch, with_tools)
    try:
        timings = []
        for _ in range(6):
            conv = (await org.admin.client.post(f"{C}/conversations", json={}, headers=h(org.admin))).json()[
                "id"
            ]
            timings.append(await _ask(org.admin, conv))
    finally:
        await org.admin.client.aclose()
    assert _p95(timings[1:]) <= target, timings  # the first turn warms the per-process caches


@pytest.mark.skipif(
    os.environ.get("COPILOT_PERF_WORKERS") != "1", reason="needs worker-conversations (non-eager)"
)
async def test_concurrent_users_against_workers(
    app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    users = int(os.environ.get("COPILOT_PERF_USERS", "50"))
    org = await make_org(app)
    _install(tmp_path, monkeypatch, True)
    members = [org.admin] + [await join(app, org.tenant, name=f"User {i}") for i in range(users - 1)]
    convs = [(await m.client.post(f"{C}/conversations", json={}, headers=h(m))).json()["id"] for m in members]
    try:
        timings = await asyncio.gather(*(_ask(m, c) for m, c in zip(members, convs, strict=True)))
    finally:
        for m in members:
            await m.client.aclose()
    assert _p95(list(timings)) <= 2.0, sorted(timings)[
        -5:
    ]  # POST + turn.started; deltas follow on the stream

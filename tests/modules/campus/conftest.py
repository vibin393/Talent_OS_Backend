from collections.abc import Iterator

import pytest

from app.core import bot_challenge, rate_limit
from tests.epic3 import _isolated_files, _no_real_ai, fake_ai  # noqa: F401
from tests.modules.placements.conftest import dorg, other_dorg  # noqa: F401


@pytest.fixture(autouse=True)
def _public_env() -> Iterator[None]:
    rate_limit.memory_backend().reset()
    bot_challenge.set_verifier(None)
    yield
    bot_challenge.set_verifier(None)
    rate_limit.memory_backend().reset()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Load tests (``@pytest.mark.slow``) only run with ``-m slow`` (mirrors ``tests/perf/conftest.py``)."""
    if "slow" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="load test: run with -m slow")
    here = __file__.rsplit("conftest.py", 1)[0]
    for item in items:
        if "slow" in item.keywords and str(item.fspath).startswith(here):
            item.add_marker(skip)

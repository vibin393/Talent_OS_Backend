"""Performance tests run only when selected with ``-m perf`` (they seed 100k rows)."""

import pytest

from tests.epic3 import _isolated_files, _no_real_ai  # noqa: F401
from tests.modules.demands.helpers import dorg  # noqa: F401


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "perf" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="performance test: run with -m perf")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip)

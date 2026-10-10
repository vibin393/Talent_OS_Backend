"""The Conformance Kit over every registered Connector Manifest (Epic 66, spec 0.9; run at every merge)."""

import pytest

from app.core.hooks import autodiscover
from app.core.integrations import manifests, validate_catalogue
from tests.connectors.conformance import run_conformance

autodiscover()
MANIFESTS = {m.key: m for m in manifests()}


def test_catalogue_validates() -> None:
    assert {"fake_job_board", "fake_hrms", "fake_stt"} <= set(MANIFESTS)
    assert validate_catalogue() == []


@pytest.mark.parametrize("key", sorted(MANIFESTS))
async def test_manifest_conforms(key: str) -> None:
    assert await run_conformance(MANIFESTS[key]) == []


@pytest.mark.parametrize("key", sorted(MANIFESTS))
async def test_manifest_conforms_ext(key: str) -> None:
    """Epic 69 (spec A8): manual fallback, retry and rate-limit classification for every manifest."""
    from tests.connectors.conformance_ext import run_conformance_ext

    assert await run_conformance_ext(MANIFESTS[key]) == []

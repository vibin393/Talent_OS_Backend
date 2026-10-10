import pytest

from app.services import ecosystem_service, oauth_service
from tests.epic12 import _isolated_files, _privacy_env, fake_ai, org3, other3  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    oauth_service.clear_cache()
    ecosystem_service.bust_cache()

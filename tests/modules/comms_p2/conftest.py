from collections.abc import Iterator

import pytest

from app.services import custom_domains
from app.services.connectors.sms.fake import FakeSmsProvider
from app.services.connectors.whatsapp.fake import FakeWhatsAppProvider
from tests.epic3 import _isolated_files, _no_real_ai  # noqa: F401
from tests.epic10 import dorg, mailbox, other_dorg, outbox  # noqa: F401
from tests.modules.interviews.helpers import _connections_env  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_channels() -> Iterator[None]:
    FakeSmsProvider.reset()
    FakeWhatsAppProvider.reset()
    custom_domains.clear_cache()
    yield
    FakeSmsProvider.reset()
    FakeWhatsAppProvider.reset()
    custom_domains.set_resolver(None)
    custom_domains.clear_cache()

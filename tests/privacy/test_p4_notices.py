# ruff: noqa: E501
"""Epic 65 story 65.7 (FR-324): English notices for the five Phase 4 consent purposes. Each body states that an AI
assistant is involved, that the model provider is a sub-processor processing data outside India, and how to
withdraw; ``active_notice`` returns an active ``en`` notice on a fresh tenant.

``PENDING_NOTICE_GAPS`` (shrink-only, empty at phase close) records owner notices that miss a required statement; the
owner fixes the text (Epic 65 does not edit owner notices) and removes the entry.
"""

import re

import pytest

from app.core.database import tenant_session
from app.core.hooks import autodiscover
from app.services.privacy import consent
from tests.epic12 import Org3

autodiscover("app.services")

P4_NOTICE_OWNERS = {
    "ai_agent_communication": "Epic 57",
    "voice_screening": "Epic 58",
    "call_recording": "Epic 58",
    "interview_recording": "Epic 60",
    "knowledge_assistant": "Epic 62",
}
STATEMENTS = {
    "ai": re.compile(r"\bAI\b|artificial intelligence", re.IGNORECASE),
    "sub_processor": re.compile(
        r"sub-?processor.*outside India|outside India.*sub-?processor", re.IGNORECASE | re.DOTALL
    ),
    "withdraw": re.compile(r"withdraw", re.IGNORECASE),
}
# (purpose, statement) -> owner: reported to the owner epics through the coordinator.
PENDING_NOTICE_GAPS: dict[tuple[str, str], str] = {}


@pytest.mark.parametrize("purpose", sorted(P4_NOTICE_OWNERS))
def test_default_notice_text(purpose: str) -> None:
    owner = P4_NOTICE_OWNERS[purpose]
    assert purpose in consent.DEFAULT_NOTICES, f"{owner}: no default notice for {purpose}"
    title, body = consent.DEFAULT_NOTICES[purpose]
    assert title.strip() and body.strip(), f"{owner}: empty notice for {purpose}"
    missing = sorted(
        name
        for name, rx in STATEMENTS.items()
        if not rx.search(body) and (purpose, name) not in PENDING_NOTICE_GAPS
    )
    assert missing == [], f"{owner}: the {purpose} notice must state {missing}"
    fixed = sorted(
        name for (p, name) in PENDING_NOTICE_GAPS if p == purpose and STATEMENTS[name].search(body)
    )
    assert fixed == [], f"remove {purpose} {fixed} from PENDING_NOTICE_GAPS (the owner fixed the text)"


def test_pending_notice_gaps_empty_at_close() -> None:
    if PENDING_NOTICE_GAPS:
        pytest.xfail(f"open owner notice gaps: {sorted(PENDING_NOTICE_GAPS)}")


async def test_active_notices_on_a_fresh_tenant(org3: Org3) -> None:
    async with tenant_session(org3.tenant_id) as db:
        for purpose in P4_NOTICE_OWNERS:
            notice = await consent.active_notice(db, org3.tenant_id, purpose)
            assert notice is not None and notice.locale == "en", purpose

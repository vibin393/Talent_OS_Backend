"""Epic 36 (spec 0.3 / 0.7, A4): Phase 3 consent purposes and sources; ``register_default_notice`` seeds lazily."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.models.privacy import CONSENT_PURPOSES, CONSENT_SOURCES, ConsentNotice
from app.services.privacy import consent
from tests.epic12 import Org3, create_candidate

P3_PURPOSES = ("assessment", "demographic_self_id", "referral", "campus", "immigration", "tracking")
P3_SOURCES = ("campus_registration", "event_checkin", "referral_link", "self_id_form")


@pytest.fixture
def _restore_notices() -> Iterator[None]:
    saved = dict(consent.DEFAULT_NOTICES)
    yield
    consent.DEFAULT_NOTICES.clear()
    consent.DEFAULT_NOTICES.update(saved)


def test_constants_widened() -> None:
    assert set(P3_PURPOSES) <= set(CONSENT_PURPOSES)
    assert set(P3_SOURCES) <= set(CONSENT_SOURCES)


def test_register_default_notice_refuses_unknown_purpose() -> None:
    with pytest.raises(ValidationFailed) as exc:
        consent.register_default_notice("astrology", "Title", "Body")
    assert exc.value.code == "unknown_purpose"


@pytest.mark.usefixtures("_restore_notices")
async def test_register_default_notice_seeds_lazily_and_db_accepts_p3(org3: Org3) -> None:
    consent.register_default_notice("campus", "Campus hiring", "First text")
    consent.register_default_notice("campus", "Campus hiring", "We use your registration to run the drive.")
    assert consent.DEFAULT_NOTICES["campus"][1].startswith("We use your registration")
    cand = await create_candidate(org3.admin)
    cid = uuid.UUID(cand["id"])
    async with tenant_session(org3.tenant_id) as db:
        await consent.ensure_default_notices(db, org3.tenant_id)
        notice = (
            await db.execute(
                select(ConsentNotice).where(ConsentNotice.purpose == "campus", ConsentNotice.locale == "en")
            )
        ).scalar_one()
        assert notice.body_md == "We use your registration to run the drive."
        for purpose, source in zip(("campus", "referral", "assessment", "tracking"), P3_SOURCES, strict=True):
            consent.register_default_notice(purpose, f"{purpose} notice", "Body text.")
            await consent.ensure_default_notices(db, org3.tenant_id)
            row = await consent.record(db, cid, purpose, source, 1, RequestContext(tenant_id=org3.tenant_id))
            assert (row.purpose, row.source) == (purpose, source)
            assert (await consent.require(db, cid, purpose)).id == row.id

# ruff: noqa: E501
"""Epic 43: erasure handler ``campus.records`` and the 3,000-registration load (``-m slow``)."""

import asyncio
import os
import time
import uuid

import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.schemas.campus_public import PublicRegisterIn
from app.services.campus import registration
from app.services.privacy import registry
from app.services.privacy.registry import ErasureContext
from tests.modules.campus.helpers import answers, ready_drive, register, registrations, scalar, token
from tests.modules.demands.helpers import DOrg
from tests.modules.identity.conftest import new_client

N = int(os.environ.get("CAMPUS_LOAD_N", "3000"))


async def test_erasure_deletes_registrations_and_checkins(app: FastAPI, dorg: DOrg) -> None:
    handler = next(h for h in registry.handlers() if h.name == "campus.records")
    assert {"registrations", "event_checkins", "queue_entries", "batch_slots", "institution_contacts"} <= set(handler.tables)
    drive = await ready_drive(dorg)
    anon = new_client(app)
    await register(anon, token(drive["institutions"][0]["registration_url"]), email="erase@college.example.edu")
    await anon.aclose()
    reg = (await registrations(dorg, drive["id"]))[0]
    cid = uuid.UUID(reg["candidate_id"])
    async with tenant_session(dorg.tenant_id) as db:
        ectx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cid, run_id=uuid.uuid4(), reason="dsr")
        exported = await handler.export(db, ectx)
        assert exported and exported["campus_registrations"][0]["status"] == "eligible"
        counts = await handler.erase(db, ectx)
        assert counts["registrations"] == 1
        assert await handler.verify(db, ectx) == []
    assert await scalar(dorg, "SELECT count(*) FROM registrations") == 0
    funnel = (await dorg.admin.client.get(f"/api/v1/campus/drives/{drive['id']}/funnel")).json()
    assert funnel["rows"] == []


@pytest.mark.slow
async def test_three_thousand_registrations_in_ten_minutes(dorg: DOrg) -> None:
    drive = await ready_drive(dorg, institutions=3)
    tokens = [token(i["registration_url"]) for i in drive["institutions"]]
    view = await registration.public_view(tokens[0], ip="198.51.100.1")
    started = time.monotonic()
    sem = asyncio.Semaphore(8)

    async def one(n: int) -> str:
        async with sem:
            out = await registration.public_register(
                tokens[n % 3],
                PublicRegisterIn(full_name=f"Load Student {n}", email=f"load{n}@college.example.edu", answers=answers(cgpa=f"{6 + (n % 40) / 10:.2f}"), notice_id=view.notice.notice_id, agreed=True),
                {},
                ip=f"10.{n // 65536 % 256}.{n // 256 % 256}.{n % 256}",
                user_agent="load",
            )
            return out.status

    results = await asyncio.gather(*(one(n) for n in range(N)))
    elapsed = time.monotonic() - started
    assert results.count("registered") == N
    assert elapsed < 600, f"{N} registrations took {elapsed:.0f}s"
    assert await scalar(dorg, "SELECT count(*) FROM registrations WHERE drive_id = :d", d=drive["id"]) == N
    assert await scalar(dorg, "SELECT count(*) FROM consents WHERE purpose = 'campus'") == N

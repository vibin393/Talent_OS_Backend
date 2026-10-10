# ruff: noqa: E501
"""Epic 15 story 2.1 (A3): lead CSV import -- limits, validation, duplicates, idempotent re-upload, chunk isolation."""

import uuid
from typing import Any

import pytest

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.services.crm import imports
from tests.modules.crm.conftest import API, CrmOrg, create_client, create_lead, err, h, ok

HEADER = "company_name,website,contact_email,contact_phone,source_code,expected_annual_revenue\n"


async def _upload(org: CrmOrg, body: str, **form: Any) -> Any:
    files = {"file": ("leads.csv", body.encode("utf-8"), "text/csv")}
    data = {k: str(v) for k, v in form.items()}
    return await org.admin.client.post(f"{API}/leads/import", files=files, data=data, headers=h(org.admin))


async def test_template_download(corg: CrmOrg) -> None:
    resp = await corg.admin.client.get(f"{API}/leads/import-template.csv")
    assert resp.status_code == 200
    assert resp.text.startswith("company_name,")


async def test_import_mixed_rows_duplicates_and_reupload(corg: CrmOrg) -> None:
    await create_client(corg.admin, "Existing Client")
    await create_lead(corg.admin, "Existing Lead", website="https://existing-lead.com")
    body = (
        "﻿" + HEADER
        + "Alpha Ltd,alpha.com,a@alpha.com,9812345678,website,100.50\n"
        + ",,,,,\n"  # blank row: ignored
        + ",nameless.com,,,,\n"  # missing company
        + "Bad Mail,,not-an-email,,,\n"
        + "Bad Phone,,,123,,\n"
        + "Bad Source,,,,martian,\n"
        + "Bad Revenue,,,,,12.345\n"
        + "existing client,,,,,\n"  # duplicate of a client
        + "Fresh Co,https://www.existing-lead.com/x,,,,\n"  # duplicate (domain) of a lead
        + "Alpha Ltd,,,,,\n"  # duplicate of an earlier row
        + "Beta Ltd,,,,referral,\n"
    )  # fmt: skip
    resp = await _upload(corg, body)
    assert resp.status_code == 202, resp.text
    batch = resp.json()
    got = await ok(await corg.admin.client.get(f"{API}/lead-imports/{batch['batch_id']}"))
    assert got["status"] == "completed"
    assert got["created"] == 2
    assert got["skipped_duplicates"] == 3
    codes = {(e["row"], e["field"], e["code"]) for e in got["errors"]}
    assert (4, "company_name", "required") in codes
    assert (5, "contact_email", "invalid_email") in codes
    assert (6, "contact_phone", "invalid_phone") in codes
    assert (7, "source_code", "unknown_source") in codes
    assert (8, "expected_annual_revenue", "invalid_decimal") in codes
    assert {(9, "company_name", "duplicate"), (10, "company_name", "duplicate"), (11, "company_name", "duplicate")} <= codes
    page = await ok(await corg.admin.client.get(f"{API}/leads", params={"q": "Ltd"}))
    names = sorted(x["company_name"] for x in page["items"])
    assert names == ["Alpha Ltd", "Beta Ltd"]
    alpha = next(x for x in page["items"] if x["company_name"] == "Alpha Ltd")
    assert alpha["contact_phone"] == "+919812345678" and alpha["expected_annual_revenue"] == "100.50"
    assert alpha["status"] == "new" and alpha["human_id"].startswith("LEAD-")
    csv_errors = await corg.admin.client.get(f"{API}/lead-imports/{batch['batch_id']}/errors.csv")
    assert csv_errors.status_code == 200 and "invalid_email" in csv_errors.text
    # same file again -> the first batch, nothing new
    again = await _upload(corg, body)
    assert again.status_code == 202
    assert again.json()["batch_id"] == batch["batch_id"]
    assert again.json()["duplicate_of"] == batch["batch_id"]
    assert (await ok(await corg.admin.client.get(f"{API}/leads", params={"q": "Ltd"})))["total"] == 2


async def test_import_without_skipping_duplicates(corg: CrmOrg) -> None:
    await create_client(corg.admin, "Dupe Inc")
    resp = await _upload(corg, HEADER + "Dupe Inc,,,,,\n", skip_duplicates="false")
    got = await ok(await corg.admin.client.get(f"{API}/lead-imports/{resp.json()['batch_id']}"))
    assert got["created"] == 1 and got["skipped_duplicates"] == 0
    assert [e["code"] for e in got["errors"]] == ["duplicate"]


async def test_import_limits(corg: CrmOrg) -> None:
    rows = "".join(f"Company {i}\n" for i in range(5001))
    assert err(await _upload(corg, "company_name\n" + rows)) == (422, "import_too_large")
    assert err(await _upload(corg, "name\nfoo\n")) == (422, "import_columns")


@pytest.mark.slow
async def test_import_5000_rows(corg: CrmOrg) -> None:
    rows = "".join(f"Bulk Company {i}\n" for i in range(5000))
    resp = await _upload(corg, "company_name\n" + rows)
    assert resp.status_code == 202, resp.text
    got = await ok(await corg.admin.client.get(f"{API}/lead-imports/{resp.json()['batch_id']}"))
    assert (got["created"], got["errors"]) == (5000, [])


async def test_chunk_failure_isolation(corg: CrmOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imports, "CHUNK", 2)
    calls = {"n": 0}
    real = imports._reserve_ids  # noqa: SLF001

    async def flaky(db: Any, n: int) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return await real(db, n)

    monkeypatch.setattr(imports, "_reserve_ids", flaky)
    resp = await _upload(corg, "company_name\nC1\nC2\nC3\nC4\nC5\n")
    got = await ok(await corg.admin.client.get(f"{API}/lead-imports/{resp.json()['batch_id']}"))
    assert got["created"] == 3
    assert [(e["row"], e["code"]) for e in got["errors"]] == [(4, "chunk_failed"), (5, "chunk_failed")]


async def test_run_import_is_idempotent(corg: CrmOrg) -> None:
    resp = await _upload(corg, "company_name\nOnce Only\n")
    batch_id = uuid.UUID(resp.json()["batch_id"])
    ctx = RequestContext(tenant_id=corg.tenant_id, actor_kind="system")
    async with tenant_session(corg.tenant_id) as db:
        assert await imports.run_import(ctx, db, batch_id) == {"skipped": "completed"}

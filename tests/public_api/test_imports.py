# ruff: noqa: E501
"""Epic 67 (story 67.5): public bulk import -- CSV and JSONL, row cap, scan failure, duplicates counted, error report,
Sandbox cap, the ``bulk_import.completed`` outbox row."""

import json

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core import scanning
from app.core.database import tenant_session
from app.core.settings import get_settings
from tests.epic3 import Org3
from tests.public_api.e67 import Api, app_token, code, installed, user_token


def _csv(rows: list[dict[str, str]]) -> bytes:
    head = "full_name,email,current_title\n"
    return (head + "".join(f"{r['full_name']},{r.get('email', '')},{r.get('current_title', '')}\n" for r in rows)).encode()


async def _upload(api: Api, data: bytes, fmt: str = "csv", resource: str = "candidates", basis: str | None = "notice_provided"):  # noqa: ANN202
    form = {"resource": resource, "format": fmt}
    if basis:
        form["consent_basis"] = basis
    return await api.post("/imports", None, files={"file": (f"rows.{fmt}", data, "text/plain")}, data=form)


async def test_csv_import_counts_and_error_report(app: FastAPI, org3: Org3) -> None:
    inst = await installed(org3)
    api = Api(app, await user_token(inst, org3.admin.user_id))
    rows = [{"full_name": "Imported One", "email": "one@example.com"}, {"full_name": "Imported One", "email": "one@example.com"},
            {"full_name": "", "email": "bad"}]  # fmt: skip
    resp = await _upload(api, _csv(rows))
    assert resp.status_code == 202, resp.text
    job = resp.json()
    got = (await api.get(f"/imports/{job['id']}")).json()
    assert got["status"] == "completed", got
    assert got["counts"]["created"] == 2 and got["counts"]["failed"] == 1 and got["counts"]["duplicates"] >= 1
    report = await api.get(f"/imports/{job['id']}/errors")
    assert report.status_code == 200 and report.text.splitlines()[0] == "row,field,error"
    assert report.text.splitlines()[1].startswith("3,")
    async with tenant_session(org3.tenant_id) as db:
        n = (await db.execute(text("SELECT count(*) FROM outbox_events WHERE event_type = 'bulk_import.completed'"))).scalar_one()
    assert n == 1


async def test_jsonl_and_validation(app: FastAPI, org3: Org3, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = await installed(org3)
    api = Api(app, await app_token(inst))
    lines = "\n".join(json.dumps({"full_name": f"J {n}", "owner_id": str(org3.admin.user_id)}) for n in range(3)).encode()
    resp = await _upload(api, lines, fmt="jsonl")
    assert resp.status_code == 202, resp.text
    assert (await api.get(f"/imports/{resp.json()['id']}")).json()["counts"]["created"] == 3
    no_basis = await _upload(api, lines, fmt="jsonl", basis=None)
    assert code(no_basis) == "consent_basis_required"
    monkeypatch.setattr(get_settings(), "public_api_bulk_max_rows", 2)
    too_many = await _upload(api, lines, fmt="jsonl")
    assert (too_many.status_code, code(too_many)) == (422, "too_many_rows")


async def test_scan_failure_marks_failed(app: FastAPI, org3: Org3, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = await installed(org3)
    api = Api(app, await app_token(inst))

    async def infected(data: bytes, filename: str) -> scanning.ScanResult:
        return scanning.ScanResult("infected", "EICAR")

    monkeypatch.setattr(scanning, "scan", infected)
    resp = await _upload(api, _csv([{"full_name": "X"}]))
    assert resp.status_code == 202 and resp.json()["status"] == "failed"


async def test_sandbox_cap(app: FastAPI, org3: Org3, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.public_api import imports

    async def sandbox(_tid: object) -> bool:
        return True

    monkeypatch.setattr(imports, "is_sandbox", sandbox)
    monkeypatch.setattr(get_settings(), "sandbox_import_cap", 1)
    inst = await installed(org3)
    api = Api(app, await app_token(inst))
    resp = await _upload(api, _csv([{"full_name": "A"}, {"full_name": "B"}]))
    assert (resp.status_code, code(resp)) == (422, "sandbox_import_cap")

"""Rate cards (FR-90): versions, publish supersede / overlap, immutability, templates, CSV import (Epic 16 A5)."""  # noqa: E501

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from tests.modules.commercials.helpers import API, COrg, active_contract, err, line, ok, published_card

CSV_HEADER = "role,location,experience_min_years,experience_max_years,min_bill_rate,max_bill_rate,currency,rate_unit,max_vendor_rate,markup_pct\n"  # noqa: E501


async def test_publish_supersedes_and_overlap(corg: COrg) -> None:
    v1 = await published_card(corg, effective_from="2026-01-01")
    assert v1["status"] == "published" and v1["version"] == 1 and v1["human_id"].startswith("RC-")
    draft = await ok(await corg.c.post(f"{API}/rate-cards/{v1['id']}/new-version", headers=corg.h()), 201)
    assert draft["version"] == 2 and draft["status"] == "draft" and len(draft["lines"]) == 1
    # Starting on/before the current version's start overlaps.
    await ok(
        await corg.c.patch(
            f"{API}/rate-cards/{draft['id']}", json={"effective_from": "2026-01-01"}, headers=corg.h()
        )
    )
    err(
        await corg.c.post(f"{API}/rate-cards/{draft['id']}/publish", headers=corg.h()),
        409,
        "rate_card_overlap",
    )
    await ok(
        await corg.c.patch(
            f"{API}/rate-cards/{draft['id']}", json={"effective_from": "2026-11-01"}, headers=corg.h()
        )
    )
    v2 = await ok(await corg.c.post(f"{API}/rate-cards/{draft['id']}/publish", headers=corg.h()))
    assert v2["status"] == "published"
    old = await ok(await corg.c.get(f"{API}/rate-cards/{v1['id']}"))
    assert old["status"] == "superseded" and old["effective_to"] == "2026-10-31"
    eff = await ok(
        await corg.c.get(f"{API}/clients/{corg.client_id}/rate-cards/effective", params={"on": "2026-10-15"})
    )
    assert eff["id"] == v1["id"]
    eff = await ok(
        await corg.c.get(f"{API}/clients/{corg.client_id}/rate-cards/effective", params={"on": "2026-11-15"})
    )
    assert eff["id"] == v2["id"]
    listed = await ok(await corg.c.get(f"{API}/clients/{corg.client_id}/rate-cards"))
    assert {c["version"] for c in listed} == {1, 2}


async def test_published_card_is_immutable(corg: COrg) -> None:
    card = await published_card(corg)
    err(
        await corg.c.put(f"{API}/rate-cards/{card['id']}/lines", json={"lines": [line()]}, headers=corg.h()),
        409,
        "rate_card_not_draft",
    )
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(DBAPIError, match="document_immutable"):
            await db.execute(text("UPDATE rate_cards SET name = 'Hacked' WHERE id = :id"), {"id": card["id"]})
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(DBAPIError, match="document_immutable"):
            await db.execute(
                text("UPDATE rate_card_lines SET max_bill_rate = 99999 WHERE rate_card_id = :id"),
                {"id": card["id"]},
            )


async def test_contract_scoped_card_must_belong_to_client(corg: COrg) -> None:
    from tests.modules.demands.helpers import create_client

    msa = await active_contract(corg)
    other = await create_client(corg.admin)
    resp = await corg.c.post(
        f"{API}/rate-cards",
        json={"client_id": str(other), "contract_id": msa["id"], "name": "x", "effective_from": "2026-01-01"},
        headers=corg.h(),
    )
    err(resp, 422, "contract_invalid")
    scoped = await published_card(corg, contract_id=msa["id"])
    assert scoped["contract_id"] == msa["id"]
    # Client-wide and contract-scoped cards do not overlap each other.
    wide = await published_card(corg)
    assert wide["status"] == "published"


async def test_templates_copy_as_draft(corg: COrg) -> None:
    tpl = await ok(
        await corg.c.post(f"{API}/rate-card-templates", json={"name": "IT standard"}, headers=corg.h()), 201
    )
    await ok(
        await corg.c.put(
            f"{API}/rate-cards/{tpl['id']}/lines",
            json={"lines": [line(), line(role="QA Engineer", location=None)]},
            headers=corg.h(),
        )
    )
    copied = await ok(
        await corg.c.post(
            f"{API}/rate-card-templates/{tpl['id']}/copy",
            json={"client_id": str(corg.client_id), "effective_from": "2026-02-01"},
            headers=corg.h(),
        ),
        201,
    )
    assert (
        copied["status"] == "draft"
        and copied["source_template_id"] == tpl["id"]
        and len(copied["lines"]) == 2
    )
    templates = await ok(await corg.c.get(f"{API}/rate-card-templates"))
    assert [t["id"] for t in templates] == [tpl["id"]]


async def test_csv_import_errors_and_idempotency(corg: COrg) -> None:
    card = await ok(
        await corg.c.post(
            f"{API}/rate-cards",
            json={"client_id": str(corg.client_id), "name": "Imported", "effective_from": "2026-01-01"},
            headers=corg.h(),
        ),
        201,
    )
    url = f"{API}/rate-cards/{card['id']}/import"
    bad = (
        CSV_HEADER + "Java Developer,Bengaluru,3,6,1200,1800,INR,hour,,\n,Pune,1,2,abc,100,INR,week,,\n"
    ).encode()
    out = await ok(await corg.c.post(url, files={"file": ("rates.csv", bad, "text/csv")}, headers=corg.h()))
    assert out["applied"] == 0
    assert {(e["row"], e["field"]) for e in out["errors"]} == {
        (3, "role"),
        (3, "min_bill_rate"),
        (3, "rate_unit"),
    }
    # skip_invalid applies the valid rows.
    out = await ok(
        await corg.c.post(
            url,
            files={"file": ("rates.csv", bad, "text/csv")},
            data={"skip_invalid": "true"},
            headers=corg.h(),
        )
    )
    assert out["applied"] == 1 and len(out["errors"]) == 3
    good = (CSV_HEADER + "QA Engineer,,0,10,800,1200,INR,hour,700,10\n").encode()
    first = await ok(await corg.c.post(url, files={"file": ("qa.csv", good, "text/csv")}, headers=corg.h()))
    again = await ok(await corg.c.post(url, files={"file": ("qa.csv", good, "text/csv")}, headers=corg.h()))
    assert first["applied"] == 1 and first["duplicate"] is False
    assert again["duplicate"] is True and again["applied"] == 1
    detail = await ok(await corg.c.get(f"{API}/rate-cards/{card['id']}"))
    assert sorted(x["role"] for x in detail["lines"]) == ["Java Developer", "QA Engineer"]


async def test_archive(corg: COrg) -> None:
    card = await published_card(corg)
    archived = await ok(await corg.c.post(f"{API}/rate-cards/{card['id']}/archive", headers=corg.h()))
    assert archived["status"] == "archived"
    unknown = await corg.c.get(f"{API}/rate-cards/{uuid.uuid4()}")
    assert unknown.status_code == 404

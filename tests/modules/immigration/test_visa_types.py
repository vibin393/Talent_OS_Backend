"""Epic 46 story 46.1 (FR-232): template seed integrity, adoption, versioning, definition validation, credential
type provisioning."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.models.compliance import CredentialType
from app.services.immigration import definition as defs
from app.services.immigration.templates import load_templates
from tests.modules.demands.helpers import DOrg, h
from tests.modules.immigration.helpers import IMM, adopt, open_case


def test_seed_has_three_valid_templates() -> None:
    templates = {t["key"]: t for t in load_templates()}
    assert set(templates) == {"us_h1b", "uk_skilled_worker", "ae_employment"}
    assert {t["country"] for t in templates.values()} == {"US", "GB", "AE"}
    for t in templates.values():
        d = defs.validate(t["definition"])
        assert all(s.category in defs.STAGE_CATEGORIES for s in d.stages)
        assert sum(1 for f in d.expiry_fields if f.primary) == 1
        assert d.alert_offsets == [120, 90, 60, 30]
        assert str(t["last_reviewed"]) == "2026-10-06"
        assert "not legal advice" in t["notice"]


def _definition(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "stages": [{"key": "prep", "label": "Prep", "category": "preparing"}],
        "expiry_fields": [{"key": "valid_to", "label": "Valid to", "primary": True, "required": True}],
    }
    d.update(over)
    return d


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"stages": [{"key": "x", "label": "X", "category": "approved"}]}, "visa_stage_category_invalid"),
        (
            {
                "stages": [
                    {"key": "x", "label": "X", "category": "filed"},
                    {"key": "x", "label": "Y", "category": "filed"},
                ]
            },
            "visa_stage_duplicate",
        ),
        ({"expiry_fields": [{"key": "a", "label": "A"}]}, "visa_expiry_primary_invalid"),
        ({"alert_offsets": [30, 30]}, "visa_alert_offsets_invalid"),
        ({"alert_offsets": [400]}, "visa_alert_offsets_invalid"),
        ({"stages": []}, "visa_definition_invalid"),
    ],
)
def test_definition_rules(over: dict[str, Any], code: str) -> None:
    with pytest.raises(ValidationFailed) as exc:
        defs.validate(_definition(**over))
    assert exc.value.code == code


def test_offsets_sorted_descending() -> None:
    assert defs.validate(_definition(alert_offsets=[30, 120, 60])).alert_offsets == [120, 60, 30]


async def test_templates_listed_with_review_date(dorg: DOrg) -> None:
    resp = await dorg.admin.client.get(f"{IMM}/visa-templates")
    assert resp.status_code == 200, resp.text
    rows = {r["key"]: r for r in resp.json()}
    assert rows["uk_skilled_worker"]["last_reviewed"] == "2026-10-06"
    assert rows["uk_skilled_worker"]["country"] == "GB"


async def test_adopt_twice_needs_a_new_name_and_provisions_credential_type(dorg: DOrg) -> None:
    vt = await adopt(dorg, "uk_skilled_worker")
    assert vt["template_last_reviewed"] == "2026-10-06"
    assert "verify with your counsel" in vt["disclaimer"]
    assert [s["category"] for s in vt["definition"]["stages"]][:2] == ["preparing", "preparing"]
    assert vt["current_version_no"] == 1 and vt["credential_type_key"] == "permit.uk_skilled_worker"
    again = await dorg.admin.client.post(
        f"{IMM}/visa-templates/uk_skilled_worker/adopt", json={}, headers=h(dorg.admin)
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "visa_type_exists"
    other = await adopt(dorg, "uk_skilled_worker", name="UK Skilled Worker (London)")
    assert other["key"] == "uk_skilled_worker_london"
    async with tenant_session(dorg.tenant_id) as db:
        t = (
            await db.execute(select(CredentialType).where(CredentialType.key == "permit.uk_skilled_worker"))
        ).scalar_one()
    assert t.category == "visa_work_permit" and t.number_kind == "encrypted"
    assert list(t.alert_offsets or []) == [120, 90, 60, 30]
    assert t.has_expiry and not t.requires_document


async def test_edit_creates_version_and_open_cases_keep_theirs(dorg: DOrg) -> None:
    vt = await adopt(dorg, "ae_employment")
    case = await open_case(dorg, vt["id"])
    d = vt["definition"]
    d["stages"].append({"key": "visa_collected", "label": "Visa collected", "category": "pending_decision"})
    d["alert_offsets"] = [60, 30]
    resp = await dorg.admin.client.put(
        f"{IMM}/visa-types/{vt['id']}/definition", json={"definition": d}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_version_no"] == 2 and [v["version_no"] for v in body["versions"]] == [2, 1]
    detail = (await dorg.admin.client.get(f"{IMM}/cases/{case['id']}")).json()
    assert detail["visa_type_version_no"] == 1
    assert "visa_collected" not in [s["key"] for s in detail["stages"]]
    moved = await dorg.admin.client.post(
        f"{IMM}/cases/{case['id']}/stage", json={"stage_key": "visa_collected"}, headers=h(dorg.admin)
    )
    assert moved.status_code == 422 and moved.json()["error"]["code"] == "visa_stage_unknown"
    async with tenant_session(dorg.tenant_id) as db:
        t = (
            await db.execute(select(CredentialType).where(CredentialType.key == "permit.ae_employment"))
        ).scalar_one()
    assert list(t.alert_offsets or []) == [60, 30]


async def test_bad_category_rejected_by_api(dorg: DOrg) -> None:
    vt = await adopt(dorg, "us_h1b")
    d = vt["definition"]
    d["stages"][0]["category"] = "decided"
    resp = await dorg.admin.client.put(
        f"{IMM}/visa-types/{vt['id']}/definition", json={"definition": d}, headers=h(dorg.admin)
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "visa_stage_category_invalid"


async def test_create_custom_patch_archive(dorg: DOrg) -> None:
    body = {
        "key": "sg_employment_pass",
        "name": "Singapore Employment Pass",
        "country": "SG",
        "definition": _definition(),
    }
    resp = await dorg.admin.client.post(f"{IMM}/visa-types", json=body, headers=h(dorg.admin))
    assert resp.status_code == 201, resp.text
    vid = resp.json()["id"]
    resp = await dorg.admin.client.patch(
        f"{IMM}/visa-types/{vid}",
        json={"name": "SG EP", "last_reviewed": "2026-10-01"},
        headers=h(dorg.admin),
    )
    assert resp.json()["name"] == "SG EP" and resp.json()["last_reviewed"] == "2026-10-01"
    resp = await dorg.admin.client.post(f"{IMM}/visa-types/{vid}/archive", headers=h(dorg.admin))
    assert resp.json()["archived_at"] is not None
    resp = await dorg.admin.client.get(f"{IMM}/visa-types")
    assert resp.status_code == 200, resp.text
    listed = resp.json()
    assert vid not in [v["id"] for v in listed]
    cand = uuid.uuid4()
    resp = await dorg.admin.client.post(
        f"{IMM}/cases", json={"candidate_id": str(cand), "visa_type_id": vid}, headers=h(dorg.admin)
    )
    assert resp.status_code == 422

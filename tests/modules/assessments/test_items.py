# ruff: noqa: E501
"""Epic 40 A3/A4: item bank (per-kind validation, no harness keys, regex vetting, versioning, import) and assembly
(publish freezes versions, item edits never change a published version, draw pool rules)."""

import io

import pytest
from openpyxl import Workbook

from app.models.assessments import ITEM_KINDS
from tests.modules.assessments.helpers import ASM, ITEMS, create_assessment, create_item, item_body, published
from tests.modules.demands.helpers import DOrg, h


@pytest.mark.parametrize("kind", ITEM_KINDS)
async def test_every_kind_validates_and_versions(dorg: DOrg, kind: str) -> None:
    item = await create_item(dorg, kind)
    assert item["kind"] == kind and item["current_version"] == 1 and item["human_id"].startswith("ITM-")
    body = item_body(kind, stem="Edited stem")
    body.pop("kind")
    r = await dorg.admin.client.put(f"{ITEMS}/{item['id']}", json=body, headers=h(dorg.admin))
    assert r.status_code == 200, r.text
    assert r.json()["current_version"] == 2 and r.json()["version"]["stem"] == "Edited stem"
    versions = (await dorg.admin.client.get(f"{ITEMS}/{item['id']}/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["stem"] != "Edited stem"  # the old version never changes


@pytest.mark.parametrize("bad_key", ["harness", "tests", "stdin", "runner", "expected_execution"])
async def test_execution_config_keys_are_refused(dorg: DOrg, bad_key: str) -> None:
    for kind in ("code_output", "code_fill", "code_find_bug"):
        body = item_body(kind)
        body["answer_key"] = {**body["answer_key"], bad_key: {"cmd": "python main.py"}}
        r = await dorg.admin.client.post(ITEMS, json=body, headers=h(dorg.admin))
        assert r.status_code == 422, r.text
    body = item_body("code_freeform")
    body["options"] = {bad_key: "x"}
    assert (await dorg.admin.client.post(ITEMS, json=body, headers=h(dorg.admin))).status_code == 422


@pytest.mark.parametrize("pattern", ["(a+)+", "(\\w+\\s?)*$", "(a)\\1", "(?P<x>a)(?P=x)", "x" * 201, "(unclosed"])
async def test_regex_vetting(dorg: DOrg, pattern: str) -> None:
    body = item_body("code_output", answer_key={"accepted": [], "regex": pattern})
    r = await dorg.admin.client.post(ITEMS, json=body, headers=h(dorg.admin))
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    ("kind", "patch"),
    [
        ("single", {"answer_key": {"correct": "z"}}),
        ("true_false", {"options": {"choices": [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}, {"id": "c", "text": "z"}]}, "answer_key": {"correct": "a"}}),
        ("code_fill", {"answer_key": {"blanks": {"2": {"accepted": ["x"]}}}}),
        ("code_order", {"answer_key": {"order": ["l1", "l2"]}}),
        ("code_find_bug", {"answer_key": {"bug_lines": [9]}}),
        ("essay", {"answer_key": {"accepted": ["x"]}}),
        ("short_text", {"answer_key": {"accepted": []}}),
    ],
)
async def test_cross_field_validation(dorg: DOrg, kind: str, patch: dict) -> None:
    r = await dorg.admin.client.post(ITEMS, json=item_body(kind, **patch), headers=h(dorg.admin))
    assert r.status_code == 422, r.text


async def test_archive_and_list(dorg: DOrg) -> None:
    item = await create_item(dorg, "single", tags=["python", "basics"])
    listed = (await dorg.admin.client.get(ITEMS, params={"q": "basics"})).json()
    assert [i["id"] for i in listed] == [item["id"]]
    r = await dorg.admin.client.post(f"{ITEMS}/{item['id']}/archive", headers=h(dorg.admin))
    assert r.status_code == 200 and r.json()["archived_at"]
    assert (await dorg.admin.client.get(ITEMS)).json() == []
    body = item_body("single")
    body.pop("kind")
    assert (await dorg.admin.client.put(f"{ITEMS}/{item['id']}", json=body, headers=h(dorg.admin))).status_code == 422


async def test_import_preview_and_commit_csv_and_xlsx(dorg: DOrg) -> None:
    csv_text = (
        "kind,stem,points,difficulty,tags,options,answer,code,rubric\n"
        "single,2+2?,1,2,math,3;4;5,2,,\n"
        "multi,Primes?,1,3,math,2;4;5,1;3,,\n"
        "numeric,pi,1,3,,,3.14|0.01,,\n"
        "short_text,Capital of France,1,1,,,Paris;paris,,\n"
        "code_output,What prints?,2,3,py,,2,print(1+1),\n"
        "code_fill,Fill,1,3,py,,1=range,for i in {{blank:1}}(3): pass,\n"
        "code_order,Order,1,3,py,def f():;    return 1,,,\n"
        "code_find_bug,Bug,1,3,py,,2,a = 1\\nb = 0\\nprint(a / b),\n"
        "essay,Explain,5,3,,,,,Clarity:3;Depth:2\n"
        "single,Bad answer,1,2,,3;4,7,,\n"
        "mystery,Unknown kind,1,1,,,,,\n"
    )
    r = await dorg.admin.client.post(f"{ITEMS}/import", files={"file": ("items.csv", csv_text.encode(), "text/csv")}, headers=h(dorg.admin))
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["valid_count"] == 9 and preview["error_count"] == 2, preview
    bad = [row for row in preview["rows"] if not row["valid"]]
    assert {row["row"] for row in bad} == {11, 12} and all(row["errors"] for row in bad)
    c = await dorg.admin.client.post(f"{ITEMS}/import/{preview['batch_id']}/commit", headers=h(dorg.admin))
    assert c.status_code == 200 and c.json()["created"] == 9
    again = await dorg.admin.client.post(f"{ITEMS}/import/{preview['batch_id']}/commit", headers=h(dorg.admin))
    assert again.status_code == 409
    wb = Workbook()
    ws = wb.active
    ws.append(["kind", "stem", "points", "options", "answer"])
    ws.append(["true_false", "Sky is blue", 1, "True;False", 1])
    buf = io.BytesIO()
    wb.save(buf)
    x = await dorg.admin.client.post(f"{ITEMS}/import", files={"file": ("items.xlsx", buf.getvalue(), "application/octet-stream")}, headers=h(dorg.admin))
    assert x.status_code == 200 and x.json()["valid_count"] == 1, x.text


# ---------------------------------------------------------------- assembly / publish (A4)
async def test_publish_freezes_versions_and_later_edits_do_not_change_it(dorg: DOrg) -> None:
    a_item = await create_item(dorg, "single")
    b_item = await create_item(dorg, "code_output")
    a = await published(dorg, [{"key": "s1", "title": "Basics", "time_limit_minutes": 10, "items": [{"item_id": a_item["id"]}, {"item_id": b_item["id"], "points": "3"}]}])
    v = a["version"]
    assert v["version"] == 1 and v["total_points"] == "4.00" and v["sections"][0]["item_count"] == 2
    body = item_body("single", stem="CHANGED")
    body.pop("kind")
    await dorg.admin.client.put(f"{ITEMS}/{a_item['id']}", json=body, headers=h(dorg.admin))
    detail = (await dorg.admin.client.get(f"{ASM}/{a['id']}")).json()
    assert detail["status"] == "published" and detail["current_version"]["id"] == v["id"]
    from app.core.database import tenant_session
    from app.models.assessments import AssessmentVersion

    async with tenant_session(dorg.tenant_id) as db:
        frozen = await db.get(AssessmentVersion, __import__("uuid").UUID(v["id"]))
        assert frozen is not None
        assert frozen.sections[0]["items"][0]["item_version_id"] == a_item["version"]["id"]


async def test_draw_pool_rules(dorg: DOrg) -> None:
    for _ in range(3):
        await create_item(dorg, "single", tags=["sql"], difficulty=2)
    a = await create_assessment(dorg)
    too_many = {"sections": [{"key": "d", "title": "Draw", "draw": {"tags": ["sql"], "difficulty": [1, 5], "count": 4}}]}
    await dorg.admin.client.put(f"{ASM}/{a['id']}/draft", json=too_many, headers=h(dorg.admin))
    r = await dorg.admin.client.post(f"{ASM}/{a['id']}/publish", headers=h(dorg.admin))
    assert r.status_code == 422 and r.json()["error"]["code"] == "draw_pool_too_small"
    ok = {"sections": [{"key": "d", "title": "Draw", "draw": {"tags": ["sql"], "difficulty": [1, 5], "count": 2}}]}
    await dorg.admin.client.put(f"{ASM}/{a['id']}/draft", json=ok, headers=h(dorg.admin))
    p = await dorg.admin.client.post(f"{ASM}/{a['id']}/publish", headers=h(dorg.admin))
    assert p.status_code == 200, p.text
    assert p.json()["warnings"] and p.json()["version"]["sections"][0]["pool_size"] == 3


async def test_publish_validation(dorg: DOrg) -> None:
    a = await create_assessment(dorg)
    assert (await dorg.admin.client.post(f"{ASM}/{a['id']}/publish", headers=h(dorg.admin))).status_code == 422
    both = {"sections": [{"key": "x", "title": "X", "items": [], "draw": {"count": 1}}]}
    assert (await dorg.admin.client.put(f"{ASM}/{a['id']}/draft", json=both, headers=h(dorg.admin))).status_code == 422
    prov = await create_assessment(dorg, name="Provider", delivery="provider")
    r = await dorg.admin.client.post(f"{ASM}/{prov['id']}/publish", headers=h(dorg.admin))
    assert r.status_code == 422 and r.json()["error"]["code"] == "provider_test_required"

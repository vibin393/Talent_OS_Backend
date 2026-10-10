"""Email templates (story 10.1, FR-54): seeding, merge-field validation, sanitising, languages, preview."""

import uuid

import pytest
from fastapi import FastAPI

from app.services.comms import merge_fields
from app.services.comms.defaults import DEFAULT_TEMPLATES
from app.services.comms.html import sanitize_html
from tests.epic10 import TEMPLATES, DOrg, candidate, count, h, member


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "key": "custom",
        "name": f"Follow-up {uuid.uuid4().hex[:6]}",
        "subject": "Hello {{ candidate.first_name }}",
        "body_html": "<p>Hi {{ candidate.first_name }}, from {{ sender.name }}</p>",
    }
    body.update(overrides)
    return body


def test_default_templates_only_use_allowed_fields() -> None:
    for key, spec in DEFAULT_TEMPLATES.items():
        merge_fields.validate_tokens(key, spec["subject"], spec["body_html"])


def test_sanitizer_strips_script_and_event_handlers() -> None:
    dirty = (
        '<p onclick="steal()">Hi</p><script>alert(1)</script>'
        '<a href="javascript:alert(1)">x</a><img src="http://evil/x.png"><img src="https://ok/x.png" onerror="x()">'  # noqa: E501
        '<a href="{{ link.url }}">go</a>'
    )
    clean = sanitize_html(dirty)
    assert "<script" not in clean and "onclick" not in clean and "onerror" not in clean
    assert "javascript:" not in clean and "http://evil" not in clean
    assert 'src="https://ok/x.png"' in clean
    assert "{{ link.url }}" in clean


async def test_system_templates_are_seeded_lazily(dorg: DOrg) -> None:
    resp = await dorg.admin.client.get(TEMPLATES)
    assert resp.status_code == 200, resp.text
    system = [t for t in resp.json() if t["is_system"]]
    assert sorted(t["key"] for t in system) == sorted(DEFAULT_TEMPLATES)
    assert all(t["languages"] == ["en"] for t in system)
    # idempotent
    resp = await dorg.admin.client.get(TEMPLATES)
    assert len([t for t in resp.json() if t["is_system"]]) == len(DEFAULT_TEMPLATES)


async def test_unknown_merge_field_is_rejected_and_not_saved(dorg: DOrg) -> None:
    before = await count(dorg.tenant_id, "email_templates")
    resp = await dorg.admin.client.post(
        TEMPLATES, json=_body(body_html="<p>{{ candidate.salary }}</p>"), headers=h(dorg.admin)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unknown_merge_field"
    assert resp.json()["error"]["details"]["fields"] == ["candidate.salary"]
    assert await count(dorg.tenant_id, "email_templates") == before


async def test_group_not_allowed_for_key(dorg: DOrg) -> None:
    resp = await dorg.admin.client.post(
        TEMPLATES,
        json=_body(key="rejection", body_html='<a href="{{ interview.join_link }}">join</a>'),
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unknown_merge_field"
    assert resp.json()["error"]["details"]["fields"] == ["interview.join_link"]


async def test_malformed_token(dorg: DOrg) -> None:
    resp = await dorg.admin.client.post(
        TEMPLATES, json=_body(subject="Hi {{ candidate.first_name"), headers=h(dorg.admin)
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "malformed_merge_token"


async def test_create_sanitizes_and_variants_bump_version(dorg: DOrg) -> None:
    resp = await dorg.admin.client.post(
        TEMPLATES,
        json=_body(body_html='<p onmouseover="x()">Hi {{ candidate.first_name }}</p><script>bad()</script>'),
        headers=h(dorg.admin),
    )
    assert resp.status_code == 201, resp.text
    tpl = resp.json()
    (variant,) = tpl["variants"]
    assert (
        variant["version"] == 1
        and "<script" not in variant["body_html"]
        and "onmouseover" not in variant["body_html"]
    )

    resp = await dorg.admin.client.put(
        f"{TEMPLATES}/{tpl['id']}/variants/en",
        json={"subject": "New subject", "body_html": "<p>Updated</p>"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["variants"][0]["version"] == 2

    resp = await dorg.admin.client.put(
        f"{TEMPLATES}/{tpl['id']}/variants/fr",
        json={"subject": "Bonjour", "body_html": "<p>Salut</p>"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "language_not_supported"

    resp = await dorg.admin.client.post(TEMPLATES, json=_body(language="fr"), headers=h(dorg.admin))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "language_not_supported"


async def test_system_template_cannot_be_removed(dorg: DOrg) -> None:
    templates = (await dorg.admin.client.get(TEMPLATES)).json()
    system = next(t for t in templates if t["is_system"])
    resp = await dorg.admin.client.post(f"{TEMPLATES}/{system['id']}/archive", headers=h(dorg.admin))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "template_system"
    # ... but it is editable
    resp = await dorg.admin.client.patch(
        f"{TEMPLATES}/{system['id']}", json={"description": "Edited"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200 and resp.json()["description"] == "Edited"

    custom = (await dorg.admin.client.post(TEMPLATES, json=_body(), headers=h(dorg.admin))).json()
    resp = await dorg.admin.client.post(f"{TEMPLATES}/{custom['id']}/archive", headers=h(dorg.admin))
    assert resp.status_code == 200 and resp.json()["archived_at"] is not None
    listed = (await dorg.admin.client.get(TEMPLATES)).json()
    assert custom["id"] not in {t["id"] for t in listed}


async def test_preview_renders_samples_and_persists_nothing(dorg: DOrg) -> None:
    before = (await count(dorg.tenant_id, "email_templates"), await count(dorg.tenant_id, "email_messages"))
    resp = await dorg.admin.client.post(
        f"{TEMPLATES}/preview",
        json={
            "key": "interview_invite",
            "subject": "Interview for {{ demand.title }}",
            "body_html": "<p>Hi {{ candidate.first_name }} <script>x()</script></p>",
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["used_samples"] is True
    assert out["subject"] == "Interview for Senior Data Engineer"
    assert "Priya" in out["body_html"] and "<script" not in out["body_html"]
    after = (await count(dorg.tenant_id, "email_templates"), await count(dorg.tenant_id, "email_messages"))
    assert after == before


async def test_preview_with_real_entities_escapes_values(dorg: DOrg) -> None:
    cand = await candidate(dorg.admin, full_name="Ann <b>Bold</b> Lee")
    resp = await dorg.admin.client.post(
        f"{TEMPLATES}/preview",
        json={
            "key": "custom",
            "subject": "Hi {{ candidate.first_name }}",
            "body_html": "<p>{{ candidate.full_name }} / {{ demand.title }}</p>",
            "context_refs": {"candidate_id": cand["id"]},
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["used_samples"] is False and out["subject"] == "Hi Ann"
    assert "&lt;b&gt;Bold&lt;/b&gt;" in out["body_html"]
    assert out["render_warnings"] == ["demand.title"]


async def test_merge_field_catalog_is_filtered_by_key(dorg: DOrg) -> None:
    resp = await dorg.admin.client.get(f"{TEMPLATES}/merge-fields", params={"key": "rejection"})
    assert resp.status_code == 200
    groups = {g["group"] for g in resp.json()}
    assert "interview" not in groups and "candidate" in groups
    resp = await dorg.admin.client.get(f"{TEMPLATES}/merge-fields", params={"key": "interview_invite"})
    assert "interview" in {g["group"] for g in resp.json()}


async def test_recruiter_reads_but_cannot_manage(app: FastAPI, dorg: DOrg) -> None:
    rec = await member(app, dorg, "recruiter")
    assert (await rec.client.get(TEMPLATES)).status_code == 200
    resp = await rec.client.post(TEMPLATES, json=_body(), headers=h(rec))
    assert resp.status_code == 403
    await rec.client.aclose()


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "", None),
        ("patch", "", {"name": "x"}),
        ("put", "/variants/en", {"subject": "s", "body_html": "<p>b</p>"}),
        ("post", "/archive", None),
    ],
)
async def test_cross_tenant_templates_404(
    dorg: DOrg, other_dorg: DOrg, method: str, suffix: str, body: dict[str, str] | None
) -> None:
    mine = (await dorg.admin.client.post(TEMPLATES, json=_body(), headers=h(dorg.admin))).json()
    call = getattr(other_dorg.admin.client, method)
    kwargs = {"headers": h(other_dorg.admin)}
    if body is not None:
        kwargs["json"] = body  # type: ignore[assignment]
    resp = await call(f"{TEMPLATES}/{mine['id']}{suffix}", **kwargs)
    assert resp.status_code == 404, resp.text

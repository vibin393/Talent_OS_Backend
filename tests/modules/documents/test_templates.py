"""P2-9.1: templates, strict merge catalogue, sandbox, preview, Legal Entity variants, renderer move."""

import uuid

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.documents import DocumentVersion
from app.models.legal_entities import LegalEntity
from app.services.documents import merge, render, templates
from app.services.offers import letter_pdf
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.documents.helpers import BODY, DOCS, candidate_with_email, published_template, send_request


async def test_unknown_merge_field_is_422_on_publish(dorg: DOrg) -> None:
    m = dorg.admin
    resp = await m.client.post(
        f"{DOCS}/templates",
        json={"name": "Bad", "kind": "nda", "body_md": "Salary {{ worker.salary }} for {{ boss.name }}"},
        headers=h(m),
    )
    assert resp.status_code == 201, resp.text
    vid = resp.json()["versions"][0]["id"]
    pub = await m.client.post(f"{DOCS}/templates/versions/{vid}/publish", headers=h(m))
    assert pub.status_code == 422
    err = pub.json()["error"]
    assert err["code"] == "unknown_merge_field"
    assert err["details"]["fields"] == ["boss.name", "worker.salary"]


async def test_sandbox_escapes_are_refused(dorg: DOrg) -> None:
    m = dorg.admin
    created = await m.client.post(
        f"{DOCS}/templates", json={"name": "T", "kind": "other"}, headers=h(m)
    )
    tid = created.json()["id"]
    for body in ("{{ ''.__class__ }}", "{{ worker.full_name.__class__.__mro__ }}", "{{ cycler() }}", "{% for x in y %}{% endfor %}", "{{ worker.full_name | attr('x') }}"):
        resp = await m.client.post(f"{DOCS}/templates/{tid}/versions", json={"body_md": body}, headers=h(m))
        if resp.status_code == 201:
            pub = await m.client.post(f"{DOCS}/templates/versions/{resp.json()['id']}/publish", headers=h(m))
            assert pub.status_code == 422, (body, pub.text)
        else:
            assert resp.status_code == 422, (body, resp.text)


async def test_preview_and_catalogue(dorg: DOrg) -> None:
    m = dorg.admin
    template = await published_template(m)
    vid = template["current_version_id"]
    preview = await m.client.post(f"{DOCS}/templates/versions/{vid}/preview", headers=h(m))
    assert preview.status_code == 200 and preview.headers["content-type"] == "application/pdf"
    text = render.pdf_text(preview.content)
    assert "Asha Rao" in text and "INR 1,200.00" in text
    cat = (await m.client.get(f"{DOCS}/merge-fields")).json()
    assert "salary" not in cat["namespaces"]["worker"] and cat["filters"] == ["date", "money", "upper"]
    assert "aadhaar" not in str(cat).lower()


async def test_publish_never_changes_generated_documents(dorg: DOrg) -> None:
    m = dorg.admin
    template = await published_template(m)
    cid, _ = await candidate_with_email(dorg)
    req = await send_request(dorg, template["id"], cid, send=False)
    async with tenant_session(dorg.tenant_id) as db:
        before = await db.get(DocumentVersion, uuid.UUID(req["document_version_id"]))
        assert before is not None and before.source == "generated"
        sha = before.sha256
    v2 = await m.client.post(
        f"{DOCS}/templates/{template['id']}/versions", json={"body_md": "# New wording {{ worker.full_name }}"}, headers=h(m)
    )
    assert (await m.client.post(f"{DOCS}/templates/versions/{v2.json()['id']}/publish", headers=h(m))).status_code == 200
    async with tenant_session(dorg.tenant_id) as db:
        after = await db.get(DocumentVersion, uuid.UUID(req["document_version_id"]))
        assert after is not None and after.sha256 == sha
    detail = (await m.client.get(f"{DOCS}/templates/{template['id']}")).json()
    assert [v["status"] for v in detail["versions"]] == ["published", "retired"]


async def test_legal_entity_variant_resolution(dorg: DOrg) -> None:
    m = dorg.admin
    generic = await published_template(m)
    from app.services.legal_entities.service import provision_default_entity

    async with tenant_session(dorg.tenant_id) as db:
        entity_id = (await db.execute(select(LegalEntity.id))).scalars().first()
        if entity_id is None:
            entity_id = (await provision_default_entity(db, dorg.tenant)).id
    assert entity_id is not None
    variant = await published_template(m, BODY + "\nVariant clause.", legal_entity_id=str(entity_id))
    async with tenant_session(dorg.tenant_id) as db:
        ctx = admin_ctx(dorg)
        assert await templates.resolve_template(ctx, db, "employment_agreement", entity_id) == uuid.UUID(variant["id"])
        assert await templates.resolve_template(ctx, db, "employment_agreement", uuid.uuid4()) == uuid.UUID(generic["id"])
        assert await templates.resolve_template(ctx, db, "employment_agreement", None) == uuid.UUID(generic["id"])


def test_letter_pdf_reexports_the_one_renderer() -> None:
    assert letter_pdf.markdown_flowables is render.markdown_flowables
    assert letter_pdf.append_pages is render.append_pages
    pdf = render.render_markdown("# Title\n\nHello **world**", header="Initech", footer="Doc", title="T")
    assert "Hello world" in render.pdf_text(pdf) and render.page_count(pdf) == 1


def test_merge_check_reports_every_unknown_field() -> None:
    assert merge.check_fields("{{ today | date }} {{ worker.full_name }}") == ["today", "worker.full_name"]

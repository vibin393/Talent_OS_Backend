# ruff: noqa: E501
"""Epic 74 (story 74.2, spec 0.7): the docs bundle is generated from the contracts; CI fails when an operation, event,
MCP tool or scope lacks documentation (beyond the shrink-only baseline) or when the committed bundle is stale. The
webhook verifier samples verify a real ``core.signing`` header."""

import importlib.util
import json
import time
from pathlib import Path
from typing import Any

from app.core import signing

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
VERIFIERS = Path(__file__).resolve().parents[1] / "webhooks" / "fixtures" / "verifiers"


def _gen() -> Any:
    spec = importlib.util.spec_from_file_location("build_dev_docs", SCRIPTS / "build_dev_docs.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_docs_coverage_and_freshness() -> None:
    gen = _gen()
    bundle = gen.build()
    new = sorted(set(gen.coverage(bundle)) - gen.baseline())
    assert new == [], "undocumented items:\n" + "\n".join(new)
    assert {t["execution_class"] for t in bundle["mcp"]["tools"]} <= {"reads", "creates", "proposes"}
    assert set(bundle["signing"]["verifiers"]) >= {"python/verify.py", "node/verify.js", "java/Verify.java", "csharp/Verify.cs"}
    assert gen.BUNDLE.read_text(encoding="utf-8") == gen.render(bundle), "docs-bundle.json is stale: run python scripts/build_dev_docs.py"
    baseline = json.loads(gen.BASELINE.read_text(encoding="utf-8"))
    assert set(baseline) >= set(gen.coverage(bundle)), "the baseline only shrinks"


def test_python_verifier_sample() -> None:
    spec = importlib.util.spec_from_file_location("verify_sample", VERIFIERS / "python" / "verify.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    body, ts = b'{"id":"evt_1"}', int(time.time())
    header = signing.signature_header([b"old-secret", b"whsec_test"], ts, body)
    assert mod.verify("whsec_test", header, body)
    assert not mod.verify("wrong", header, body)
    assert not mod.verify("whsec_test", signing.signature_header([b"whsec_test"], ts - 3600, body), body)

"""Container smoke test (Epic 14 A3): python3-saml / xmlsec import and xmlsec and lxml agree on libxml2.

A mismatch makes signature verification fail at runtime ("lxml & xmlsec libxml2 library version mismatch"),
so the image must build both against the same libxml2."""

import pytest


def test_xmlsec_and_lxml_share_libxml2() -> None:
    xmlsec = pytest.importorskip("xmlsec", reason="xmlsec wheel not installed on this machine (Windows dev)")
    from lxml import etree

    assert xmlsec.get_libxml_version() == etree.LIBXML_VERSION
    import authlib  # noqa: F401
    import onelogin.saml2.auth  # noqa: F401

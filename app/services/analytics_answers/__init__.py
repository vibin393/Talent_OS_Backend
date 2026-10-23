# ruff: file-ignore[line-too-long]
"""Analytics answers (Epic 64, FR-316..FR-318): the only package that touches ``analytics_answers`` and calls
``core.semantic`` / the P3 report builder for the Analytics agent. Facade: ``app.services.analytics_answers_service``.

Modules: ``service`` (run / validate / get / preview / saved), ``catalogue`` (compact catalogue for the model),
``describe`` (pure Definition -> plain English), ``cells`` (minimum-cell suppression), ``purge`` (retention)."""

# ruff: file-ignore[line-too-long]
"""Public API v1 adapters (Epic 67, AD-77, spec 0.6): map the ``schemas.public_v1`` contract onto the same domain
functions the web app uses (permissions, data scope, state rules, consent and audit identical). Routers reach them
through ``app.services.public_api_service`` only."""

# ruff: noqa: E501
"""Sales CRM (Epic 15, FR-83..FR-87): leads, duplicate detection, CSV import, lead conversion, opportunities and
the pipeline, the one-step Won conversion and the sales dashboard. Import through ``app.services.crm_service``.

``crm`` sits above the P1 core domains (AD-32 edge ``crm --> P1CORE``) and above ``commercials``; nothing imports
``crm``.
"""

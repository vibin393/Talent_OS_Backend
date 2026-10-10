"""Secrets Vault package (Epic 66, AD-82). Facade: ``app.services.vault_service``.

Kept import-free: the migration ``20261009_500_05_p5_vault_move`` imports ``app.services.vault.crypto`` and
``app.services.vault.legacy_move``, which must not pull in the ORM or other services.
"""

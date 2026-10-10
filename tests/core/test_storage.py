"""AD-13 storage port: LocalStorage round trip, tenant-prefixed keys, keys escaping the root refused."""

import uuid
from pathlib import Path

import pytest

from app.core.storage import InvalidStorageKey, LocalStorage, StorageConfigError, new_key, validate_key


async def test_local_storage_roundtrip(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    tenant = uuid.uuid4()
    key = new_key(tenant)
    assert key.startswith(f"t/{tenant}/")
    await storage.put(key, b"hello", "text/plain")
    assert (tmp_path / key).read_bytes() == b"hello"
    assert await storage.get(key) == b"hello"
    assert await storage.signed_url(key, 60) is None
    await storage.delete(key)
    with pytest.raises(FileNotFoundError):
        await storage.get(key)


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "t/../../x",
        "/abs/path",
        f"t/{uuid.uuid4()}/2026/10/../../../../secret",
        f"t/{uuid.uuid4()}/2026/10/{uuid.uuid4().hex}/../x",
        r"C:\windows\system32",
        "",
    ],
)
async def test_keys_escaping_the_root_are_refused(tmp_path: Path, key: str) -> None:
    storage = LocalStorage(tmp_path)
    with pytest.raises(InvalidStorageKey):
        await storage.put(key, b"x", "text/plain")
    with pytest.raises(InvalidStorageKey):
        validate_key(key)


def test_s3_without_bucket_is_a_config_error() -> None:
    with pytest.raises(StorageConfigError):
        from app.core.storage import S3Storage

        S3Storage("")

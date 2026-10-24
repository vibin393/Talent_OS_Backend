"""File storage port (AD-13).

All files go through a ``Storage``: ``put / get / signed_url / delete`` on opaque keys. Keys are always
tenant-prefixed: ``t/<tenant_id>/<yyyy>/<mm>/<uuid>`` (``new_key``). Implementations:

* ``LocalStorage(root)`` -- files under ``settings.file_storage_dir`` (default ``Backend/data/files``).
  Keys that would resolve outside the root are refused.
* ``S3Storage(bucket, region)`` -- lazy ``boto3`` import; a missing boto3 or bucket is a configuration error.

``get_storage()`` returns the configured backend (``settings.storage_backend``); tests may swap it with
``set_storage()``.
"""

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.core.settings import get_settings

_KEY_RE = re.compile(
    r"^(?:t/[0-9a-f-]{36}/\d{4}/\d{2}/[0-9a-f]{32}"
    # Epic 12: DSR access exports, offboarding exports and the (PII-free) erasure ledger.
    r"|t/[0-9a-f-]{36}/dsr/[0-9a-f-]{36}\.zip"
    r"|platform/exports/[0-9a-f-]{36}/[0-9a-f-]{36}\.zip"
    r"|platform/erasure-ledger/\d{4}-\d{2}\.jsonl"
    # Epic 11: generated report exports; Epic 9: candidate self-service data exports (.zip).
    r"|t/[0-9a-f-]{36}/exports/[0-9a-f-]{36}\.(?:csv|xlsx|zip))$"
)
_TENANT_PREFIX_RE = re.compile(r"^t/[0-9a-f-]{36}/$")


class StorageConfigError(RuntimeError):
    """The configured storage backend cannot be used (missing library, bucket, ...)."""


class InvalidStorageKey(ValueError):  # ruff: ignore[error-suffix-on-exception-name] - domain name
    pass


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def signed_url(self, key: str, ttl: int) -> str | None: ...

    async def delete(self, key: str) -> None: ...


def new_key(tenant_id: uuid.UUID, now: datetime | None = None) -> str:
    """``t/<tenant_id>/<yyyy>/<mm>/<uuid hex>`` -- the only key shape any backend accepts."""
    ts = now or datetime.now(UTC)
    return f"t/{tenant_id}/{ts:%Y}/{ts:%m}/{uuid.uuid4().hex}"


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        msg = "invalid storage key"
        raise InvalidStorageKey(msg)
    return key


def validate_tenant_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not _TENANT_PREFIX_RE.fullmatch(prefix):
        msg = "invalid tenant storage prefix"
        raise InvalidStorageKey(msg)
    return prefix


async def delete_tenant_prefix(tenant_id: uuid.UUID) -> int:
    """Delete every stored object of a tenant (``t/<tenant_id>/``); backends without prefix deletion: 0."""
    backend = get_storage()
    deleter = getattr(backend, "delete_prefix", None)
    if deleter is None:
        return 0
    return int(await deleter(f"t/{tenant_id}/"))


class LocalStorage:
    """Files on local disk under ``root`` (dev / single node)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            msg = "storage key escapes the storage root"
            raise InvalidStorageKey(msg)
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:  # ruff: ignore[unused-method-argument]  # NOSONAR
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    async def get(self, key: str) -> bytes:  # NOSONAR
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    async def signed_url(self, key: str, ttl: int) -> str | None:  # ruff: ignore[unused-method-argument]  # NOSONAR
        self._path(key)
        return None  # local files are only served through the permission-checked download endpoint

    async def delete(self, key: str) -> None:  # NOSONAR
        path = self._path(key)
        path.unlink(missing_ok=True)

    async def delete_prefix(self, prefix: str) -> int:  # NOSONAR
        """Delete every file under a tenant prefix ``t/<tenant_id>/`` (tenant deletion); returns the count."""
        validate_tenant_prefix(prefix)
        root = (self.root / prefix).resolve()
        if not root.is_relative_to(self.root) or not root.is_dir():
            return 0
        count = 0
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
                count += 1
            elif path.is_dir():
                path.rmdir()
        root.rmdir()
        return count


class S3Storage:
    """Amazon S3 (or compatible). ``boto3`` is optional and imported lazily."""

    def __init__(self, bucket: str, region: str | None = None) -> None:
        if not bucket:
            msg = "S3_BUCKET must be set when STORAGE_BACKEND=s3"
            raise StorageConfigError(msg)
        try:
            import boto3  # type: ignore[import-not-found,unused-ignore]
        except ImportError as exc:
            msg = "STORAGE_BACKEND=s3 requires the boto3 package"
            raise StorageConfigError(msg) from exc
        self.bucket = bucket
        self._client: Any = boto3.client("s3", region_name=region or None)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        import anyio

        validate_key(key)
        await anyio.to_thread.run_sync(
            lambda: self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        )

    async def get(self, key: str) -> bytes:
        import anyio

        validate_key(key)

        def _get() -> bytes:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
            return bytes(obj["Body"].read())

        return await anyio.to_thread.run_sync(_get)

    async def signed_url(self, key: str, ttl: int) -> str | None:  # NOSONAR
        validate_key(key)
        return str(
            self._client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl
            )
        )

    async def delete(self, key: str) -> None:
        import anyio

        validate_key(key)
        await anyio.to_thread.run_sync(lambda: self._client.delete_object(Bucket=self.bucket, Key=key))

    async def delete_prefix(self, prefix: str) -> int:
        import anyio

        validate_tenant_prefix(prefix)

        def _delete() -> int:
            count = 0
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
                    count += len(keys)
            return count

        return await anyio.to_thread.run_sync(_delete)


_override: Storage | None = None
_default: Storage | None = None


def set_storage(storage: Storage | None) -> None:
    """Replace the storage backend (tests); ``None`` restores the configured one."""
    global _override  # process-wide wiring
    _override = storage


def get_storage() -> Storage:
    global _default  # process-wide singleton
    if _override is not None:
        return _override
    if _default is None:
        s = get_settings()
        if s.storage_backend == "s3":
            _default = S3Storage(s.s3_bucket, s.s3_region)
        else:
            _default = LocalStorage(s.file_storage_dir)
    return _default

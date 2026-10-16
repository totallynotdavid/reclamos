import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError
from reclamos_api.config import Settings


class ObjectNotFound(KeyError):
    pass


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes:
        """Return the object, or raise ObjectNotFound."""
        ...


class LocalStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"object key {key!r} leaves the store root")
        return path

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        del content_type
        await asyncio.to_thread(self._write, self._path(key), data)

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError:
            raise ObjectNotFound(key) from None

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rename a finished temp file into place so a reader never sees half a PDF.
        descriptor, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, path)
        except BaseException:
            os.unlink(temporary)
            raise


class S3Store:
    def __init__(self, bucket: str, *, endpoint_url: str | None = None) -> None:
        self.bucket = bucket
        self._client: Any = boto3.client("s3", endpoint_url=endpoint_url)

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def get(self, key: str) -> bytes:
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self.bucket, Key=key
            )
            return await asyncio.to_thread(response["Body"].read)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise ObjectNotFound(key) from None
            raise


def store_from_settings(settings: Settings) -> ObjectStore:
    if settings.pdf_store == "local":
        return LocalStore(Path(settings.pdf_store_path))
    if settings.pdf_store == "s3":
        if not settings.pdf_bucket:
            raise ValueError("PDF_STORE=s3 needs PDF_BUCKET")
        return S3Store(settings.pdf_bucket, endpoint_url=settings.s3_endpoint_url)
    raise ValueError(f"PDF_STORE must be 'local' or 's3', got {settings.pdf_store!r}")

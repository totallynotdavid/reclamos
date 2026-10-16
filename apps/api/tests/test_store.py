import asyncio
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto.server import ThreadedMotoServer
from reclamos_api.config import Settings
from reclamos_api.corpus.store import (
    LocalStore,
    ObjectNotFound,
    ObjectStore,
    S3Store,
    store_from_settings,
)


@pytest.fixture
def s3_endpoint(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A local S3 server, which speaks the same HTTP protocol as AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    boto3.client("s3", endpoint_url=endpoint).create_bucket(Bucket="pdfs")
    try:
        yield endpoint
    finally:
        server.stop()


def round_trip(store: ObjectStore) -> None:
    async def scenario() -> None:
        await store.put(
            "resolutions/1.pdf", b"%PDF-one", content_type="application/pdf"
        )
        await store.put(
            "resolutions/1.pdf", b"%PDF-two", content_type="application/pdf"
        )
        assert await store.get("resolutions/1.pdf") == b"%PDF-two"
        with pytest.raises(ObjectNotFound):
            await store.get("resolutions/2.pdf")

    asyncio.run(scenario())


def test_local_store_round_trip(tmp_path: Path) -> None:
    round_trip(LocalStore(tmp_path))


def test_local_store_keeps_keys_inside_its_root(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "root")

    with pytest.raises(ValueError, match="leaves the store root"):
        asyncio.run(store.put("../escape.pdf", b"x", content_type="application/pdf"))
    assert not (tmp_path / "escape.pdf").exists()


def test_s3_store_round_trip(s3_endpoint: str) -> None:
    round_trip(S3Store("pdfs", endpoint_url=s3_endpoint))


def test_settings_choose_the_store(tmp_path: Path, s3_endpoint: str) -> None:
    local = store_from_settings(
        Settings(database_url="", pdf_store="local", pdf_store_path=str(tmp_path))
    )
    s3 = store_from_settings(
        Settings(
            database_url="",
            pdf_store="s3",
            pdf_bucket="pdfs",
            s3_endpoint_url=s3_endpoint,
        )
    )

    assert isinstance(local, LocalStore)
    assert isinstance(s3, S3Store)
    round_trip(s3)


def test_s3_store_needs_a_bucket() -> None:
    with pytest.raises(ValueError, match="PDF_BUCKET"):
        store_from_settings(Settings(database_url="", pdf_store="s3"))

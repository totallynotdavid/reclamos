"""The corpus pipeline as rqueue jobs."""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import asyncpg
from reclamos_api.corpus import repository
from reclamos_api.corpus.chunk import chunk_text
from reclamos_api.corpus.extract import ExtractionError, extract_text
from reclamos_api.corpus.parse import parse_resolution
from reclamos_api.corpus.site import (
    Fetcher,
    FetchError,
    fetch_listing,
    parse_resolution_page,
    split_window,
)
from reclamos_api.corpus.store import ObjectStore
from reclamos_api.retrieval.embedder import Embedder
from rqueue import Queue, TaskContext  # pyright: ignore[reportMissingTypeStubs]

QUEUE_NAME = "reclamos"
DISCOVER = "corpus.discover"


class StageFailure(Exception):
    """The stage cannot succeed for this resolution in this process. It cannot
    succeed on a retry either, unless `transient`: another process may succeed."""

    def __init__(
        self, reason: str, detail: str = "", *, transient: bool = False
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.transient = transient


class ChunksReplaced(Exception):
    """The chunks a job embedded were replaced before it stored the vectors."""

    def __init__(self, resolution_id: int) -> None:
        super().__init__(f"chunks of resolution {resolution_id} were replaced")


class Pipeline:
    def __init__(
        self,
        pool: asyncpg.Pool,
        queue: Queue,
        store: ObjectStore,
        fetcher: Fetcher,
        embedder: Embedder,
    ) -> None:
        self.pool = pool
        self.queue = queue
        self.store = store
        self.fetcher = fetcher
        self.embedder = embedder

    def register(self) -> None:
        self.queue.register(name=DISCOVER, handler=self.discover)
        self.queue.register(
            name="corpus.download", handler=self._guarded("download", self._download)
        )
        self.queue.register(
            name="corpus.extract", handler=self._guarded("extract", self._extract)
        )
        self.queue.register(
            name="corpus.parse", handler=self._guarded("parse", self._parse)
        )
        self.queue.register(
            name="corpus.chunk", handler=self._guarded("chunk", self._chunk)
        )
        self.queue.register(
            name="corpus.embed", handler=self._guarded("embed", self._embed)
        )

    async def enqueue_discovery(self, start: date, end: date) -> None:
        async with self.pool.acquire() as connection:
            await self._enqueue_discover(connection, start, end)

    async def enqueue_unfinished(self) -> int:
        async with self.pool.acquire() as connection:
            ids = await repository.unfinished_ids(connection)
            for resolution_id in ids:
                await self._advance(
                    connection, await repository.get(connection, resolution_id)
                )
        return len(ids)

    async def _enqueue_discover(self, connection: Any, start: date, end: date) -> None:
        await self.queue.enqueue(
            connection,
            task=DISCOVER,
            payload={"start": start.isoformat(), "end": end.isoformat()},
            dedupe_key=f"{DISCOVER}:{start}:{end}",
            on_conflict="return_existing",
        )

    async def _advance(self, connection: Any, row: Any) -> None:
        stage = repository.next_stage(row)
        if stage is None:
            return
        await self.queue.enqueue(
            connection,
            task=f"corpus.{stage}",
            payload={"id": row["id"]},
            dedupe_key=f"corpus.{stage}:{row['id']}",
            on_conflict="return_existing",
        )

    async def discover(self, payload: dict[str, str], context: TaskContext) -> None:
        del context
        start, end = (
            date.fromisoformat(payload["start"]),
            date.fromisoformat(payload["end"]),
        )
        listing = await fetch_listing(self.fetcher, start, end)
        async with self.pool.acquire() as connection, connection.transaction():
            if listing.total > len(listing.items):
                if start == end:
                    # Retrying cannot show more entries, so the window is
                    # recorded as failed and the job is done.
                    await repository.record_listing_failure(
                        connection,
                        start,
                        end,
                        reason="listing_truncated",
                        detail=f"{listing.total} entries on {start}, "
                        f"the page shows {len(listing.items)}",
                    )
                    return
                for window_start, window_end in split_window(start, end):
                    await self._enqueue_discover(connection, window_start, window_end)
                return
            await repository.clear_listing_failures(connection, start, end)
            await repository.upsert_listed(connection, listing.items)
            for item in listing.items:
                await self._advance(
                    connection, await repository.get(connection, item.id)
                )

    def _guarded(
        self, stage: str, body: Callable[[int], Awaitable[None]]
    ) -> Callable[[dict[str, int], TaskContext], Awaitable[None]]:
        async def handler(payload: dict[str, int], context: TaskContext) -> None:
            resolution_id = payload["id"]
            try:
                await body(resolution_id)
            except (StageFailure, ExtractionError, FetchError) as failure:
                if _is_transient(failure) and not context.is_last_attempt:
                    raise
                await self._record_failure(resolution_id, stage, failure)
            except Exception as error:
                if context.will_retry(error):
                    raise
                await self._record_failure(resolution_id, stage, error)
                raise

        return handler

    async def _record_failure(
        self, resolution_id: int, stage: str, error: Exception
    ) -> None:
        reason = getattr(error, "reason", f"error_{type(error).__name__}")
        detail = getattr(error, "detail", "") or str(error)
        async with self.pool.acquire() as connection:
            await repository.record_failure(
                connection, resolution_id, stage=stage, reason=reason, detail=detail
            )

    async def _download(self, resolution_id: int) -> None:
        row = await self._row(resolution_id)
        if row["downloaded_at"] is None:
            try:
                response = await self.fetcher.get(row["page_url"])
                page = parse_resolution_page(response.text, base_url=row["page_url"])
            except ValueError as error:
                raise StageFailure(str(error)) from None
            except FetchError as error:
                raise FetchError(
                    f"page_{error.reason}", permanent=error.permanent
                ) from None
            try:
                pdf = await self.fetcher.get(page.pdf_url)
            except FetchError as error:
                raise FetchError(
                    f"pdf_{error.reason}", permanent=error.permanent
                ) from None
            if not pdf.content.startswith(b"%PDF"):
                raise StageFailure("not_a_pdf", pdf.headers.get("content-type", ""))
            key = f"resolutions/{resolution_id}.pdf"
            await self.store.put(key, pdf.content, content_type="application/pdf")
            async with self.pool.acquire() as connection:
                await repository.record_download(
                    connection,
                    resolution_id,
                    summary=page.summary,
                    pdf_url=page.pdf_url,
                    pdf_key=key,
                    sha256=hashlib.sha256(pdf.content).hexdigest(),
                    size=len(pdf.content),
                )
        await self._advance_resolution(resolution_id)

    async def _extract(self, resolution_id: int) -> None:
        row = await self._row(resolution_id)
        if row["extracted_at"] is None:
            extracted = await extract_text(await self.store.get(row["pdf_key"]))
            async with self.pool.acquire() as connection:
                await repository.record_extraction(
                    connection,
                    resolution_id,
                    body=extracted.text,
                    method=extracted.method,
                    pages=extracted.pages,
                    ocr_pages=extracted.ocr_pages,
                )
        await self._advance_resolution(resolution_id)

    async def _parse(self, resolution_id: int) -> None:
        async with self.pool.acquire() as connection:
            row = await repository.get(connection, resolution_id)
            if row["parsed_at"] is None:
                await repository.record_parse(
                    connection,
                    resolution_id,
                    parse_resolution(row["body"], summary=row["summary"] or ""),
                )

    async def _chunk(self, resolution_id: int) -> None:
        row = await self._row(resolution_id)
        if row["chunked_at"] is None:
            chunks = chunk_text(row["body"])
            async with self.pool.acquire() as connection:
                await repository.record_chunks(connection, resolution_id, chunks)
        await self._advance_resolution(resolution_id)

    async def _embed(self, resolution_id: int) -> None:
        row = await self._row(resolution_id)
        # With no chunks there is nothing to embed: the chunk job queues this
        # stage when it finishes.
        if row["chunked_at"] is not None and row["embedded_at"] is None:
            async with self.pool.acquire() as connection:
                chunks = await repository.chunk_texts(connection, resolution_id)
            vectors = await self.embedder.embed_passages(
                [chunk["text"] for chunk in chunks]
            )
            async with self.pool.acquire() as connection:
                stored = await repository.record_embeddings(
                    connection,
                    resolution_id,
                    self.embedder.model,
                    [(c["id"], v) for c, v in zip(chunks, vectors, strict=True)],
                )
            if stored == "other_model":
                # A process that runs the index's model, the CLI or a redeployed
                # worker, can claim the retry. Only the last attempt records it.
                raise StageFailure(
                    "other_model",
                    f"embedded with {self.embedder.model}, "
                    "but the index holds vectors of another model",
                    transient=True,
                )
            if stored == "chunks_replaced":
                # Fails the attempt, and rqueue runs the job again on the new
                # chunks. Returning would leave this job holding the stage's
                # dedupe key, so nothing could queue the stage again.
                raise ChunksReplaced(resolution_id)
        await self._advance_resolution(resolution_id)

    async def _row(self, resolution_id: int) -> Any:
        """Read a resolution and give the connection back, so a stage that waits
        on the network, OCR or the model holds none. A worker that cannot get a
        connection cannot renew its leases."""
        async with self.pool.acquire() as connection:
            return await repository.get(connection, resolution_id)

    async def _advance_resolution(self, resolution_id: int) -> None:
        async with self.pool.acquire() as connection:
            await self._advance(
                connection, await repository.get(connection, resolution_id)
            )


def _is_transient(failure: Exception) -> bool:
    if isinstance(failure, FetchError):
        return not failure.permanent
    if isinstance(failure, StageFailure):
        return failure.transient
    return isinstance(failure, ExtractionError) and failure.reason == "tool_missing"

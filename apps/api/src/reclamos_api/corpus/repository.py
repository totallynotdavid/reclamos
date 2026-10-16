from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

from reclamos_api.corpus.chunk import Chunk
from reclamos_api.corpus.parse import Parsed
from reclamos_api.corpus.site import ListedResolution

Connection = Any


async def upsert_listed(
    connection: Connection, items: Sequence[ListedResolution]
) -> None:
    await connection.executemany(
        """
        INSERT INTO resolutions (id, page_url, expediente, published_on)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (id) DO UPDATE
        SET page_url = EXCLUDED.page_url,
            expediente = EXCLUDED.expediente,
            published_on = EXCLUDED.published_on
        """,
        [(i.id, i.page_url, i.expediente, i.published_on) for i in items],
    )


async def get(connection: Connection, resolution_id: int) -> Any:
    return await connection.fetchrow(
        "SELECT * FROM resolutions WHERE id = $1", resolution_id
    )


def next_stage(row: Any) -> str | None:
    """The first stage a resolution has not finished, or None when all are."""
    if row["downloaded_at"] is None:
        return "download"
    if row["extracted_at"] is None:
        return "extract"
    if row["chunked_at"] is None:
        return "chunk"
    if row["embedded_at"] is None:
        return "embed"
    if row["parsed_at"] is None:
        return "parse"
    return None


async def unfinished_ids(connection: Connection) -> list[int]:
    rows = await connection.fetch(
        """
        SELECT id FROM resolutions
        WHERE downloaded_at IS NULL OR extracted_at IS NULL OR parsed_at IS NULL
           OR chunked_at IS NULL OR embedded_at IS NULL
        ORDER BY id
        """
    )
    return [row["id"] for row in rows]


async def record_chunks(
    connection: Connection, resolution_id: int, chunks: Sequence[Chunk]
) -> None:
    """Replace the resolution's chunks. Their embeddings are gone with them, so
    the resolution is no longer embedded."""
    async with connection.transaction():
        await connection.execute(
            "DELETE FROM chunks WHERE resolution_id = $1", resolution_id
        )
        await connection.executemany(
            """
            INSERT INTO chunks (resolution_id, ordinal, section, page_start, page_end,
                                char_start, char_end, text)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            [
                (
                    resolution_id,
                    c.ordinal,
                    c.section,
                    c.page_start,
                    c.page_end,
                    c.char_start,
                    c.char_end,
                    c.text,
                )
                for c in chunks
            ],
        )
        await connection.execute(
            """
            UPDATE resolutions
            SET chunked_at = now(), embedded_at = NULL,
                failure_stage = NULL, failure_reason = NULL, failure_detail = NULL,
                failed_at = NULL
            WHERE id = $1
            """,
            resolution_id,
        )


async def chunk_texts(connection: Connection, resolution_id: int) -> list[Any]:
    return await connection.fetch(
        "SELECT id, text FROM chunks WHERE resolution_id = $1 ORDER BY ordinal",
        resolution_id,
    )


EmbeddingsResult = Literal["stored", "chunks_replaced", "other_model"]


async def record_embeddings(
    connection: Connection,
    resolution_id: int,
    model: str,
    vectors: Sequence[tuple[int, Sequence[float]]],
) -> EmbeddingsResult:
    """Store `model`'s vector for each (chunk id, vector) and mark the resolution
    embedded. Nothing is stored when one of the chunks has been replaced since the
    vectors were computed, or when the index holds another model's vectors."""
    try:
        async with connection.transaction():
            return await _store_embeddings(connection, resolution_id, model, vectors)
    except _ChunksReplaced:
        return "chunks_replaced"


class _ChunksReplaced(Exception):
    """Raised inside the transaction so the vectors that did find a chunk are
    rolled back with the rest."""


async def _store_embeddings(
    connection: Connection,
    resolution_id: int,
    model: str,
    vectors: Sequence[tuple[int, Sequence[float]]],
) -> EmbeddingsResult:
    await connection.execute(
        "INSERT INTO embedding_model (model) VALUES ($1) ON CONFLICT DO NOTHING",
        model,
    )
    # A shared lock, so `reset_embeddings` waits for this transaction to finish.
    held = await connection.fetchval("SELECT model FROM embedding_model FOR SHARE")
    if held != model:
        return "other_model"
    status = await connection.execute(
        """
        UPDATE chunks SET embedding = wanted.embedding::vector
        FROM unnest($1::bigint[], $2::text[]) AS wanted (id, embedding)
        WHERE chunks.id = wanted.id
        """,
        [chunk_id for chunk_id, _ in vectors],
        [vector_literal(vector) for _, vector in vectors],
    )
    if int(status.split()[-1]) != len(vectors):
        raise _ChunksReplaced
    await connection.execute(
        """
        UPDATE resolutions
        SET embedded_at = now(),
            failure_stage = NULL, failure_reason = NULL, failure_detail = NULL,
            failed_at = NULL
        WHERE id = $1
        """,
        resolution_id,
    )
    return "stored"


def vector_literal(vector: Sequence[float]) -> str:
    """The text form pgvector reads: `[0.1,0.2]`."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


async def reset_chunks(connection: Connection) -> int:
    """Forget every chunk so the next crawl chunks and embeds each resolution
    again. Returns the number of resolutions reset."""
    async with connection.transaction():
        await connection.execute("DELETE FROM chunks")
        return await _count_updated(
            connection,
            "UPDATE resolutions SET chunked_at = NULL, embedded_at = NULL "
            "WHERE chunked_at IS NOT NULL OR embedded_at IS NOT NULL",
        )


async def reset_embeddings(connection: Connection, model: str) -> int:
    """Forget every embedding so the next crawl embeds each resolution again with
    `model`. A job still running with another model stores nothing."""
    async with connection.transaction():
        await connection.execute(
            """
            INSERT INTO embedding_model (model) VALUES ($1)
            ON CONFLICT (only_row) DO UPDATE SET model = EXCLUDED.model
            """,
            model,
        )
        await connection.execute("UPDATE chunks SET embedding = NULL")
        return await _count_updated(
            connection,
            "UPDATE resolutions SET embedded_at = NULL WHERE embedded_at IS NOT NULL",
        )


async def _count_updated(connection: Connection, statement: str) -> int:
    status = await connection.execute(statement)
    return int(status.split()[-1])


async def record_download(
    connection: Connection,
    resolution_id: int,
    *,
    summary: str,
    pdf_url: str,
    pdf_key: str,
    sha256: str,
    size: int,
) -> None:
    await connection.execute(
        """
        UPDATE resolutions
        SET summary = $2, pdf_url = $3, pdf_key = $4, pdf_sha256 = $5, pdf_bytes = $6,
            downloaded_at = now(),
            failure_stage = NULL, failure_reason = NULL, failure_detail = NULL,
            failed_at = NULL
        WHERE id = $1
        """,
        resolution_id,
        summary,
        pdf_url,
        pdf_key,
        sha256,
        size,
    )


async def record_extraction(
    connection: Connection,
    resolution_id: int,
    *,
    body: str,
    method: str,
    pages: int,
    ocr_pages: int,
) -> None:
    await connection.execute(
        """
        UPDATE resolutions
        SET body = $2, text_method = $3, page_count = $4, ocr_pages = $5,
            extracted_at = now(),
            failure_stage = NULL, failure_reason = NULL, failure_detail = NULL,
            failed_at = NULL
        WHERE id = $1
        """,
        resolution_id,
        body,
        method,
        pages,
        ocr_pages,
    )


async def record_parse(
    connection: Connection, resolution_id: int, parsed: Parsed
) -> None:
    """Store what the parser found. The row counts as parsed only when nothing is missing."""
    failed = bool(parsed.missing)
    await connection.execute(
        """
        UPDATE resolutions
        SET number = $2, resolved_on = $3, operator = $4, subject = $5,
            claim_type = $6, appeal_kind = $7, outcome = $8,
            parsed_at = CASE WHEN $9::boolean THEN NULL ELSE now() END,
            failure_stage = $10::text,
            failure_reason = $11::text,
            failure_detail = $12::text,
            failed_at = CASE WHEN $9::boolean THEN now() END
        WHERE id = $1
        """,
        resolution_id,
        parsed.number,
        parsed.resolved_on,
        parsed.operator,
        parsed.subject,
        parsed.claim_type,
        parsed.appeal_kind,
        parsed.outcome,
        failed,
        "parse" if failed else None,
        f"no_{parsed.missing[0]}" if failed else None,
        parsed.detail() if failed else None,
    )


async def record_failure(
    connection: Connection, resolution_id: int, *, stage: str, reason: str, detail: str
) -> None:
    await connection.execute(
        """
        UPDATE resolutions
        SET failure_stage = $2, failure_reason = $3, failure_detail = $4, failed_at = now()
        WHERE id = $1
        """,
        resolution_id,
        stage,
        reason,
        detail[:2000],
    )


async def record_listing_failure(
    connection: Connection, start: date, end: date, *, reason: str, detail: str
) -> None:
    await connection.execute(
        """
        INSERT INTO listing_failures (start_date, end_date, reason, detail)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (start_date, end_date) DO UPDATE
        SET reason = EXCLUDED.reason, detail = EXCLUDED.detail, failed_at = now()
        """,
        start,
        end,
        reason,
        detail,
    )


async def clear_listing_failures(
    connection: Connection, start: date, end: date
) -> None:
    """Forget the failures inside a window that has now been listed in full."""
    await connection.execute(
        "DELETE FROM listing_failures WHERE start_date >= $1 AND end_date <= $2",
        start,
        end,
    )

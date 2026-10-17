"""The fixed index behind the CI gate.

A subset is some resolutions, some of their chunks with the vectors the model
made for them, and the vectors of the questions the gate asks. Loading it into
an empty database gives an index to search without downloading the model. It is
stored as gzipped JSON Lines: one `meta` line, then `resolution`, `chunk` and
`query` lines. Vectors are half-precision floats in base64.
"""

import base64
import gzip
import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from reclamos_api.corpus.repository import vector_literal
from reclamos_api.retrieval.embedder import DIMENSION

RESOLUTION_COLUMNS = (
    "id",
    "page_url",
    "expediente",
    "published_on",
    "number",
    "resolved_on",
    "operator",
    "subject",
    "claim_type",
    "appeal_kind",
    "outcome",
)
CHUNK_COLUMNS = (
    "resolution_id",
    "ordinal",
    "section",
    "page_start",
    "page_end",
    "char_start",
    "char_end",
    "text",
)
DATE_COLUMNS = ("published_on", "resolved_on")


@dataclass(slots=True)
class Subset:
    model: str
    questions: list[str]  # ids of the questions the gate asks
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[dict[str, Any]] = field(default_factory=list)  # with `embedding`
    queries: dict[str, list[float]] = field(default_factory=dict)  # text -> vector


def encode_vector(vector: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}e", *vector)).decode()


def decode_vector(text: str) -> list[float]:
    raw = base64.b64decode(text)
    return list(struct.unpack(f"<{len(raw) // 2}e", raw))


def write_subset(path: Path, subset: Subset) -> None:
    lines: list[dict[str, Any]] = [
        {"meta": {"model": subset.model, "questions": subset.questions}}
    ]
    for resolution in subset.resolutions:
        lines.append(
            {
                "resolution": {
                    key: value.isoformat() if isinstance(value, date) else value
                    for key, value in resolution.items()
                }
            }
        )
    for chunk in subset.chunks:
        lines.append(
            {"chunk": chunk | {"embedding": encode_vector(chunk["embedding"])}}
        )
    for query, vector in subset.queries.items():
        lines.append({"query": {"text": query, "embedding": encode_vector(vector)}})
    # A fixed mtime keeps the same subset byte-identical across rebuilds.
    with gzip.GzipFile(path, "wb", mtime=0) as file:
        for line in lines:
            file.write((json.dumps(line, ensure_ascii=False) + "\n").encode())


def read_subset(path: Path) -> Subset:
    subset: Subset | None = None
    with gzip.open(path, "rt", encoding="utf-8") as file:
        for line in file:
            record = json.loads(line)
            if "meta" in record:
                subset = Subset(record["meta"]["model"], record["meta"]["questions"])
            elif subset is None:
                raise ValueError(f"{path} does not start with a meta line")
            elif "resolution" in record:
                subset.resolutions.append(record["resolution"])
            elif "chunk" in record:
                chunk = record["chunk"]
                subset.chunks.append(
                    chunk | {"embedding": decode_vector(chunk["embedding"])}
                )
            else:
                query = record["query"]
                subset.queries[query["text"]] = decode_vector(query["embedding"])
    if subset is None:
        raise ValueError(f"{path} is empty")
    return subset


async def load_subset(connection: Any, subset: Subset) -> None:
    """Insert the subset into a database that has the schema and no resolutions."""
    for vector in (*subset.queries.values(), *(c["embedding"] for c in subset.chunks)):
        if len(vector) != DIMENSION:
            raise ValueError(f"a vector has {len(vector)} dimensions, not {DIMENSION}")
    async with connection.transaction():
        for resolution in subset.resolutions:
            values = [
                date.fromisoformat(v) if k in DATE_COLUMNS and v else v
                for k, v in ((k, resolution[k]) for k in RESOLUTION_COLUMNS)
            ]
            await connection.execute(
                f"""
                INSERT INTO resolutions ({", ".join(RESOLUTION_COLUMNS)},
                                         parsed_at, chunked_at, embedded_at)
                VALUES ({", ".join(f"${i}" for i in range(1, len(values) + 1))},
                        now(), now(), now())
                """,
                *values,
            )
        await connection.executemany(
            f"""
            INSERT INTO chunks ({", ".join(CHUNK_COLUMNS)}, embedding)
            VALUES ({", ".join(f"${i}" for i in range(1, len(CHUNK_COLUMNS) + 1))},
                    ${len(CHUNK_COLUMNS) + 1}::vector)
            """,
            [
                (*(chunk[c] for c in CHUNK_COLUMNS), vector_literal(chunk["embedding"]))
                for chunk in subset.chunks
            ],
        )
        await connection.execute(
            "INSERT INTO embedding_model (model) VALUES ($1)", subset.model
        )


class PrecomputedEmbedder:
    """Answers `embed_query` with the vector the subset holds for that text. It
    cannot embed anything else, so a changed question fails loudly."""

    def __init__(self, subset: Subset) -> None:
        self.model = subset.model
        self._queries = subset.queries

    async def embed_query(self, text: str) -> list[float]:
        try:
            return self._queries[text]
        except KeyError:
            raise LookupError(
                f"the subset holds no vector for the question {text!r}; "
                "rebuild it with `reclamos gate subset`"
            ) from None

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError("a subset holds its passages' vectors already")

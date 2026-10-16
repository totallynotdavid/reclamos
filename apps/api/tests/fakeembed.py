"""A deterministic stand-in for the embedding model.

Each word is hashed to a dimension, so two texts that share words get vectors
that point the same way. It needs no download and gives the same vectors on
every host.
"""

import asyncio
import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from reclamos_api.retrieval.embedder import DIMENSION


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return [w for w in re.findall(r"[a-z0-9]+", plain) if len(w) > 3]


def _vector(text: str) -> list[float]:
    vector = [0.0] * DIMENSION
    for word in _words(text):
        digest = hashlib.md5(word.encode()).digest()
        vector[int.from_bytes(digest[:4]) % DIMENSION] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


class CountingPool:
    """A pool that counts, per asyncio task, the connections handed out and not
    yet returned. Give it to the code under test and the queue the real pool, so
    the queue's own connections and other jobs running at the same time are not
    counted."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._held: Counter[asyncio.Task[Any] | None] = Counter()

    @property
    def in_use(self) -> int:
        """Connections held by the calling task."""
        return self._held[asyncio.current_task()]

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        task = asyncio.current_task()
        async with self._pool.acquire() as connection:
            self._held[task] += 1
            try:
                yield connection
            finally:
                self._held[task] -= 1


class HashingEmbedder:
    model = "hashing"

    def __init__(self) -> None:
        self.passages_embedded = 0
        # Set to a `CountingPool` to record how many connections the code under
        # test holds while a batch is embedded.
        self.pool: Any = None
        self.connections_in_use: list[int] = []

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.passages_embedded += len(texts)
        if self.pool is not None:
            self.connections_in_use.append(self.pool.in_use)
        return [_vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return _vector(text)


STALE_VECTOR = _vector("obsoleto")


class GatedEmbedder(HashingEmbedder):
    """Holds the first batch until `release` is set. That batch gets `STALE_VECTOR`
    for every passage, so a test can tell whether it was stored."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._gated = True

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if not self._gated:
            return await super().embed_passages(texts)
        self._gated = False
        self.started.set()
        await self.release.wait()
        return [STALE_VECTOR for _ in texts]

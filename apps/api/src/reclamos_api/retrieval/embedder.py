"""Text embeddings from a model that runs on this host."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from reclamos_api.config import Settings

# `chunks.embedding` in schema.sql has this many dimensions.
DIMENSION = 1024

# Texts the model reads at once. Memory grows with the batch, because every text
# is padded to the longest and attention is quadratic in its length.
BATCH_SIZE = 16


class Embedder(Protocol):
    """Turns each text into a vector of `DIMENSION` numbers. Search compares them by
    cosine, so their length does not matter."""

    model: str

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class _Prefixes:
    query: str
    passage: str


# The E5 models were trained with these markers and lose recall without them.
_PREFIXES = {
    "intfloat/multilingual-e5-large": _Prefixes("query: ", "passage: "),
}


def embedder_from_settings(settings: Settings) -> Embedder:
    return FastEmbedder(
        settings.embedding_model, cache_path=Path(settings.embedding_cache_path)
    )


class FastEmbedder:
    """An ONNX model run by fastembed. The model loads on first use and is
    downloaded into `cache_path` when it is not there yet."""

    def __init__(self, model: str, *, cache_path: Path) -> None:
        if model not in _PREFIXES:
            raise ValueError(
                f"unknown embedding model {model!r}; known: {', '.join(_PREFIXES)}"
            )
        self.model = model
        self._cache_path = cache_path
        self._prefixes = _PREFIXES[model]
        self._loaded: Any = None
        self._lock = asyncio.Lock()

    def load(self) -> Any:
        from fastembed import TextEmbedding

        return TextEmbedding(model_name=self.model, cache_dir=str(self._cache_path))

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        marked = [self._prefixes.passage + text for text in texts]
        return await self._embed(marked)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self._prefixes.query + text]))[0]

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        # One batch at a time: the model already uses every core, and each batch
        # in flight holds its own activations in memory.
        async with self._lock:
            if self._loaded is None:
                self._loaded = await asyncio.to_thread(self.load)
            engine = self._loaded
            vectors = await asyncio.to_thread(
                lambda: list(engine.embed(texts, batch_size=BATCH_SIZE))
            )
        return [vector.tolist() for vector in vectors]

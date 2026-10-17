import asyncio
import math
from pathlib import Path

import pytest
from fastembed import TextEmbedding
from reclamos_api.config import Settings
from reclamos_api.retrieval.embedder import DIMENSION, FastEmbedder

PASSAGES = [
    (
        "La empresa operadora cobró la renovación de un equipo que el reclamante "
        "nunca solicitó."
    ),
    "El reclamante pidió la portabilidad numérica de su línea a otro operador.",
    "El servicio fue cortado por falta de pago sin una notificación previa.",
]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.hypot(*a) * math.hypot(*b))


def cached_embedder() -> FastEmbedder | None:
    settings = Settings.from_environment()
    cache = Path(settings.embedding_cache_path)
    repository = next(
        entry["sources"]["hf"]
        for entry in TextEmbedding.list_supported_models()
        if entry["model"] == settings.embedding_model
    )
    if not (cache / f"models--{repository.replace('/', '--')}").is_dir():
        return None
    return FastEmbedder(settings.embedding_model, cache_path=cache)


embedder = cached_embedder()


@pytest.mark.skipif(
    embedder is None, reason="the embedding model is not in EMBEDDING_CACHE_PATH"
)
def test_the_real_model_puts_a_question_nearest_the_passage_that_answers_it() -> None:
    assert embedder is not None
    model = embedder

    async def scenario() -> tuple[list[list[float]], list[float]]:
        return (
            await model.embed_passages(PASSAGES),
            await model.embed_query("me cobraron un equipo que yo no pedí"),
        )

    passages, query = asyncio.run(scenario())

    assert {len(v) for v in [*passages, query]} == {DIMENSION}
    similarity = [cosine(query, p) for p in passages]
    assert similarity.index(max(similarity)) == 0

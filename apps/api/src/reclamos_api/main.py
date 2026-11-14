from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from reclamos_api.answers.answer import AnswerSettings
from reclamos_api.answers.api import router as answer_router
from reclamos_api.answers.llm import LanguageModel, model_from_settings
from reclamos_api.answers.spend import Ledger
from reclamos_api.config import Settings
from reclamos_api.db import database_pool
from reclamos_api.health import dependency_health
from reclamos_api.retrieval.api import router as search_router
from reclamos_api.retrieval.embedder import Embedder, embedder_from_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_environment()
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        app.state.pool = pool
        app.state.queue_schema = settings.queue_schema
        app.state.embedder = app.state.embedder or embedder_from_settings(settings)
        app.state.model = app.state.model or model_from_settings(settings)
        app.state.ledger = Ledger(pool, daily_cap_usd=settings.daily_usd_cap)
        app.state.answer_settings = AnswerSettings(
            model=settings.answer_model,
            min_similarity=settings.refusal_min_similarity,
        )
        yield


def create_app(
    embedder: Embedder | None = None, model: LanguageModel | None = None
) -> FastAPI:
    """`embedder` and `model` replace the configured models, for tests."""
    app = FastAPI(title="Reclamos API", lifespan=lifespan)
    app.state.embedder = embedder
    app.state.model = model
    app.include_router(search_router)
    app.include_router(answer_router)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        payload = await dependency_health(
            request.app.state.pool, queue_schema=request.app.state.queue_schema
        )
        status_code = 200 if payload["status"] == "ok" else 503
        return JSONResponse(status_code=status_code, content=payload)

    return app


app = create_app()

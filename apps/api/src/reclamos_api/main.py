from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
        yield


def create_app(embedder: Embedder | None = None) -> FastAPI:
    """`embedder` replaces the configured model, for tests."""
    app = FastAPI(title="Reclamos API", lifespan=lifespan)
    app.state.embedder = embedder
    app.include_router(search_router)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        payload = await dependency_health(
            request.app.state.pool, queue_schema=request.app.state.queue_schema
        )
        status_code = 200 if payload["status"] == "ok" else 503
        return JSONResponse(status_code=status_code, content=payload)

    return app


app = create_app()

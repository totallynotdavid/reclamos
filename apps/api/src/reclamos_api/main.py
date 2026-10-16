from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from reclamos_api.config import Settings
from reclamos_api.db import database_pool
from reclamos_api.health import dependency_health


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_environment()
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        app.state.pool = pool
        app.state.queue_schema = settings.queue_schema
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="Reclamos API", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        payload = await dependency_health(
            request.app.state.pool, queue_schema=request.app.state.queue_schema
        )
        status_code = 200 if payload["status"] == "ok" else 503
        return JSONResponse(status_code=status_code, content=payload)

    return app


app = create_app()

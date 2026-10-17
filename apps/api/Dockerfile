FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY packages/eval/pyproject.toml packages/eval/pyproject.toml
COPY apps/api/src apps/api/src
COPY packages/eval/src packages/eval/src
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates git poppler-utils tesseract-ocr tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --frozen --package reclamos-api --no-dev
ENV PATH="/app/.venv/bin:$PATH"
RUN python -c "from reclamos_api.config import Settings; \
from reclamos_api.retrieval.embedder import embedder_from_settings; \
embedder_from_settings(Settings.from_environment()).load()"
CMD ["uvicorn", "reclamos_api.main:app", "--host", "0.0.0.0", "--port", "8000"]

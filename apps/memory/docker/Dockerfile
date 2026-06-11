FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY apps/memory/pyproject.toml apps/memory/uv.lock apps/memory/README.md ./
# Include the local-models extra (sentence-transformers + modal) so the local
# docker-compose Prefect worker keeps full pipeline parity with local dev.
RUN uv sync --no-dev --frozen --extra local-models

COPY apps/memory/src/ src/

ENV PYTHONPATH=/app/src

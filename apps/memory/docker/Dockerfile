FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY apps/memory/pyproject.toml apps/memory/uv.lock apps/memory/README.md ./
RUN uv sync --no-dev --frozen

COPY apps/memory/src/ src/

ENV PYTHONPATH=/app/src

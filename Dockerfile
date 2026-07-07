FROM node:22-slim AS web-builder

WORKDIR /web

COPY web/package*.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UAM_WEB_DIST=/app/web/dist

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY scripts/migrate.py scripts/backup.py scripts/restore.py scripts/export_vault.py scripts/import_vault.py ./scripts/
COPY --from=web-builder /web/dist ./web/dist
RUN python -m pip install ".[api,postgres,qdrant,nats,documents]"

EXPOSE 8080

RUN useradd --create-home --uid 10001 memory
USER memory

CMD ["uvicorn", "memory_plane.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

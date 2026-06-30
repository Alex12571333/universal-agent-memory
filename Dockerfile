FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install ".[api,postgres]"

EXPOSE 8080

RUN useradd --create-home --uid 10001 memory
USER memory

CMD ["uvicorn", "memory_plane.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

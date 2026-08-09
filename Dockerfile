FROM python:3.13-alpine AS builder

RUN apk add --no-cache build-base libffi-dev linux-headers \
    && pip install --no-cache-dir uv==0.11.17

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.13-alpine

RUN apk add --no-cache ffmpeg libffi libstdc++ \
    && addgroup -S app \
    && adduser -S -G app app \
    && mkdir -p /app /data/cache \
    && chown -R app:app /app /data

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISK_CACHE_DIR=/data/cache

USER app
CMD ["python", "-m", "ttblow.main"]

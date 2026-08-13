#!/bin/sh
set -eu

IMAGE="${IMAGE:-ttblow:latest}"
CONTAINER="${CONTAINER:-ttblow}"
CACHE_VOLUME="${CACHE_VOLUME:-ttblow-cache}"

if [ ! -f .env ]; then
    echo "Не найден .env. Скопируйте .env.example в .env и заполните настройки." >&2
    exit 1
fi

echo "Building ${IMAGE}..."
docker build -t "$IMAGE" .

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo "Starting ${CONTAINER}..."
docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --env-file .env \
    --env DISK_CACHE_DIR=/data/cache \
    --volume "${CACHE_VOLUME}:/data/cache" \
    "$IMAGE" >/dev/null

echo "Started ${CONTAINER}"
docker ps --filter "name=^/${CONTAINER}$"

echo "Logs: docker logs -f ${CONTAINER}"

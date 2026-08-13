#!/bin/sh
set -eu

IMAGE="${IMAGE:-ttblow:latest}"
CONTAINER="${CONTAINER:-ttblow}"
CACHE_VOLUME="${CACHE_VOLUME:-ttblow-cache}"
COOKIES_FILE="${COOKIES_FILE:-cookies.txt}"

if [ ! -f .env ]; then
    echo "Не найден .env. Скопируйте .env.example в .env и заполните настройки." >&2
    exit 1
fi

echo "Building ${IMAGE}..."
docker build -t "$IMAGE" .

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo "Starting ${CONTAINER}..."
COOKIES_VOLUME=""
if [ -f "$COOKIES_FILE" ]; then
    COOKIES_VOLUME="--volume $(realpath "$COOKIES_FILE"):/data/cookies.txt"
fi
docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --env-file .env \
    --env DISK_CACHE_DIR=/data/cache \
    --volume "${CACHE_VOLUME}:/data/cache" \
    $COOKIES_VOLUME \
    "$IMAGE" >/dev/null

echo "Started ${CONTAINER}"
docker ps --filter "name=^/${CONTAINER}$"

echo "Logs: docker logs -f ${CONTAINER}"

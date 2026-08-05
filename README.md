# ttblow

Inline Telegram-бот для TikTok и Instagram Reels. Бот скачивает видео и превращает TikTok-фотоподборки в MP4-слайдшоу с оригинальной музыкой через `yt-dlp` и `ffmpeg`, загружает результат в закрытый Telegram-канал, а пользователю возвращает cached inline-результат.

## Как это работает

```text
TikTok → yt-dlp + proxy → /tmp → закрытый Telegram-канал
                                      ↓
                                  file_id
                                      ↓
                           inline cached video
```

Кэш состоит из двух уровней:

- `cachetools.TTLCache` — быстрый кэш в RAM;
- `diskcache` — локальный кэш на диске, переживающий перезапуск.

В кэше хранятся только platform ID и Telegram `file_id`. Видео хранится у Telegram.

## Настройка

1. В `@BotFather` включите inline mode через `/setinline`.
2. Создайте закрытый канал и добавьте бота администратором с правом публикации.
3. Создайте `.env`:

```env
TELEGRAM_BOT_TOKEN=токен_от_BotFather
YTDLP_PROXY=http://proxy.example:8080
TELEGRAM_PROXY=http://proxy.example:8080
TELEGRAM_CACHE_CHAT_ID=-1001234567890
BOT_MODE=polling
```

Для публичного канала вместо числового ID можно использовать его username:

```env
TELEGRAM_CACHE_CHAT_ID=@my_cache_channel
```

Proxy для `yt-dlp` и Telegram API опционален. Если `TELEGRAM_PROXY` не указан, для Bot API используется `YTDLP_PROXY`; если оба пустые, запросы идут напрямую.

По умолчанию одновременно обрабатываются 2 загрузки, максимальный размер видео — 50 MB, длительность — 600 секунд, лимит пользователя — 10 запросов в минуту. Inline-ответ ожидает максимум 9 секунд, после чего тяжёлая загрузка продолжается в фоне и результат появится при следующем запросе. Эти значения настраиваются через `MAX_CONCURRENT_JOBS`, `MAX_FILE_SIZE`, `MAX_DURATION`, `RATE_LIMIT_COUNT` и `RATE_LIMIT_WINDOW`.

## Запуск

Скопируйте пример конфигурации и заполните `.env`:

```bash
cp .env.example .env
```

Для production через Docker и polling:

```bash
./start.sh
```

Скрипт собирает Alpine-образ, удаляет старый контейнер, запускает новый с `restart unless-stopped` и сохраняет `diskcache` в Docker volume `ttblow-cache`.

Логи:

```bash
docker logs -f ttblow
```

Для ручного запуска без Docker нужен установленный `ffmpeg` в `PATH`:

```bash
uv run main.py
```

Проверки кода:

```bash
uv run ruff format --check main.py test_main.py
uv run ruff check main.py test_main.py
uv run python -m unittest -v test_main.py
```

Сейчас production запускается через polling. Позже polling можно заменить на webhook через `aiohttp`; для этого понадобится публичный HTTPS-адрес и обязательный `TELEGRAM_WEBHOOK_SECRET` вместе с `PUBLIC_BASE_URL` и `WEBHOOK_PATH`.

В Telegram:

```text
@имя_бота https://vm.tiktok.com/ZN8dRB5uV/
@имя_бота https://www.instagram.com/reel/ABC123/
```

Поддерживаются публичные TikTok-видео, TikTok-фотоподборки и Instagram Reels. Фотоподборки собираются в одно видео со звуком TikTok; в Docker-образ входит `ffmpeg`. Приватные Instagram-публикации и ролики, для которых Instagram требует login/cookies, не поддерживаются без отдельной настройки cookies. Временные файлы создаются в `/tmp` и удаляются после загрузки в Telegram; старые каталоги чистятся при запуске. При ошибках и таймауте бот отвечает пустым списком inline-результатов и пишет подробности только в лог. Логи выводятся в stdout; уровень задаётся через `LOG_LEVEL=INFO` или `DEBUG`.

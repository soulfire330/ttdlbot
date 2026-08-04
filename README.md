# ttblow

Inline Telegram-бот для TikTok. Бот скачивает видео через `yt-dlp` и proxy, загружает его в закрытый Telegram-канал, а пользователю возвращает `file_id` через `InlineQueryResultCachedVideo`.

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

В кэше хранятся только TikTok ID и Telegram `file_id`. Видео хранится у Telegram.

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

Для ручного запуска без Docker:

```bash
uv run main.py
```

Сейчас production запускается через polling. Позже polling можно заменить на webhook через `aiohttp`; для этого понадобится публичный HTTPS-адрес и параметры `PUBLIC_BASE_URL`, `WEBHOOK_PATH` и `TELEGRAM_WEBHOOK_SECRET`.

В Telegram:

```text
@имя_бота https://vm.tiktok.com/ZN8dRB5uV/
```

Временные файлы создаются в `/tmp` и удаляются после загрузки в Telegram. Логи выводятся в stdout; уровень задаётся через `LOG_LEVEL=INFO` или `DEBUG`.

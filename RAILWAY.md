# Деплой ZeroxAI на Railway

На Railway поднимаются **Gradio-чат** (`app.py`) и **Telegram-бот** (`telegram_bot.py`) в одном сервисе.

## Что нужно заранее

1. Аккаунт на [railway.app](https://railway.app)
2. Репозиторий на GitHub: `https://github.com/eharutyunyan717-source/ZeroxAiApp`
3. Переменные окружения:
   - `ZEROXAI_API_KEYS` — ключи Groq через запятую
   - `TELEGRAM_BOT_TOKEN` — токен от @BotFather

## Способ 1 — через сайт Railway (проще)

1. Открой [railway.app/new](https://railway.app/new)
2. **Deploy from GitHub repo** → выбери `ZeroxAiApp`
3. Railway сам найдёт `Dockerfile` и соберёт проект
4. Открой сервис → **Variables** → добавь:

```text
ZEROXAI_API_KEYS=gsk_key1,gsk_key2
TELEGRAM_BOT_TOKEN=123456:ABC...
```

5. **Settings** → **Networking** → **Generate Domain** — получишь URL вида `https://zeroxai-production.up.railway.app`
6. Дождись статуса **Active** — Gradio откроется по этому URL

## Способ 2 — через CMD (Railway CLI)

### Установка CLI

```cmd
npm install -g @railway/cli
```

Если `npm` не найден — установи [Node.js](https://nodejs.org).

### Логин и деплой

```cmd
cd "C:\Users\erikh\Documents\New project"

railway login

railway init

railway variables set ZEROXAI_API_KEYS=gsk_key1,gsk_key2
railway variables set TELEGRAM_BOT_TOKEN=123456:ABC...

railway up
```

Публичный URL:

```cmd
railway domain
```

## Обновление после изменений

### Если деплой с GitHub

```cmd
cd "C:\Users\erikh\Documents\New project"
git add app.py telegram_bot.py Dockerfile railway.toml
git commit -m "Update Railway deploy"
git push github master
```

Railway пересоберёт проект автоматически (если включён auto-deploy).

### Если деплой через CLI

```cmd
cd "C:\Users\erikh\Documents\New project"
railway up
```

## Сохранение балансов (data.json)

На Railway файловая система **сбрасывается** при перезапуске. Чтобы монеты и роли не пропадали:

1. В Railway: **Volumes** → Create Volume → mount path: `/app/data`
2. Добавь переменную:

```text
DATA_FILE=/app/data/data.json
```

## Проверка

- Gradio: открой домен Railway в браузере
- Telegram: напиши боту `/start` или `/ping`
- Логи: Railway → сервис → **Deployments** → **View Logs**

## Частые ошибки

| Ошибка | Решение |
|--------|---------|
| Build failed | Проверь, что в репо есть `Dockerfile` и `requirements.txt` |
| Application failed to respond | Подожди 1–2 мин после деплоя, Gradio долго стартует |
| Bot не отвечает | Проверь `TELEGRAM_BOT_TOKEN` в Variables |
| AI не отвечает | Проверь `ZEROXAI_API_KEYS` |

## Два сервиса (опционально)

Можно разделить на 2 сервиса в Railway:

| Сервис | Start command | Нужен домен |
|--------|---------------|-------------|
| Web (Gradio) | `python app.py` | Да |
| Bot only | `python telegram_bot.py` | Нет |

Для bot-only создай отдельный серvice с командой `python telegram_bot.py` и только токенами.

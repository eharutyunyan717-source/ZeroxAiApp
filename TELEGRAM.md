# ZeroxAI Telegram Bot

`telegram_bot.py` runs ZeroxAI as a Telegram bot with the same model and key rotation as the web app.

## Create the bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Copy the bot token.

## Environment variables

Set these variables before running:

```text
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ZEROXAI_API_KEYS=gsk_first_key,gsk_second_key,gsk_third_key
```

## Run on Windows CMD

```cmd
cd "C:\Users\erikh\Documents\New project"
set TELEGRAM_BOT_TOKEN=your_telegram_bot_token
set ZEROXAI_API_KEYS=gsk_first_key,gsk_second_key
python telegram_bot.py
```

If `python` is not found, install Python or run it with the full Python path.

## Commands

- `/start` - welcome message
- `/help` - command list
- `/clear` - clear current Telegram user history

Do not commit Telegram tokens or API keys into Git.

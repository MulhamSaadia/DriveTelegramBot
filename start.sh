#!/bin/sh
# Runs a local Telegram Bot API server (files up to 2GB) + the bot in one container.
set -e
telegram-bot-api --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" \
  --local --dir=/data --http-port=8081 &

for i in $(seq 1 30); do
  nc -z 127.0.0.1 8081 && break
  sleep 1
done

export BOT_API_URL=http://127.0.0.1:8081
exec python bot.py

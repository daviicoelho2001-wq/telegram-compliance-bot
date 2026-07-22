#!/bin/sh
set -e

if [ "$TELEGRAM_LOCAL_BOT_API" = "true" ]; then
  if [ -z "$TELEGRAM_API_ID" ] || [ -z "$TELEGRAM_API_HASH" ]; then
    echo "TELEGRAM_LOCAL_BOT_API=true mas faltou TELEGRAM_API_ID/TELEGRAM_API_HASH — pegue em https://my.telegram.org/apps"
    exit 1
  fi
  echo "Iniciando servidor local do Telegram Bot API na porta 8081..."
  telegram-bot-api --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" --local --http-port=8081 &
  sleep 2
fi

exec python bot.py

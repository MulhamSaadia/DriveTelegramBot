FROM aiogram/telegram-bot-api:latest AS api

FROM python:3.12-alpine
RUN apk add --no-cache libstdc++ openssl zlib
COPY --from=api /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py user_drive.py start.sh ./
RUN chmod +x start.sh && mkdir -p /data

# env needed: BOT_TOKEN, ADMIN_IDS, DATABASE_URL, GOOGLE_TOKEN_JSON,
#             TELEGRAM_API_ID, TELEGRAM_API_HASH (from my.telegram.org)
CMD ["./start.sh"]

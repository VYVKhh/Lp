FROM aiogram/telegram-bot-api:latest AS tgapi

FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libssl3 zlib1g libstdc++6 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tgapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
RUN mkdir -p /app/data

# إنشاء سكربت التشغيل داخل الصورة
RUN printf '%s\n' \
'#!/bin/sh' \
'(' \
'  while true; do' \
'    telegram-bot-api --local --dir=/app/data --http-port=8081 --api-id="$TG_API_ID" --api-hash="$TG_API_HASH"' \
'    echo "telegram-bot-api stopped, restarting in 3s..."' \
'    sleep 3' \
'  done' \
') &' \
'i=0' \
'until curl -s -o /dev/null http://127.0.0.1:8081/ || [ $i -ge 30 ]; do' \
'  i=$((i+1)); sleep 1' \
'done' \
'exec python bot.py' \
> /app/start.sh && chmod +x /app/start.sh

CMD ["/app/start.sh"]

FROM aiogram/telegram-bot-api:latest

USER root
RUN apk add --no-cache python3 py3-pip curl

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY bot.py .
RUN mkdir -p /app/data

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
'exec python3 bot.py' \
> /app/start.sh && chmod +x /app/start.sh

ENTRYPOINT []
CMD ["/app/start.sh"]

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

WORKDIR /app
COPY bot.py store.py tgapi.py ./

RUN useradd --system --uid 10001 --home-dir /app bot \
    && mkdir -p /data \
    && chown bot:bot /data

USER bot
VOLUME ["/data"]
ENTRYPOINT ["python3", "bot.py", "--data-dir", "/data"]

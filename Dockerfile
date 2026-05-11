FROM python:3.11-slim

WORKDIR /app

# Системные либы для psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-webhook.txt ./
RUN pip install --no-cache-dir -r requirements-webhook.txt

COPY dexbot/ ./dexbot/

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# Healthcheck — fly.io проверит /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1

CMD ["uvicorn", "dexbot.webhook_server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

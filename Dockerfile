FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY data/sample_events.json ./data/sample_events.json

ENV DATABASE_PATH=/app/data/payments.db \
    SEED_FILE=/app/data/sample_events.json \
    SEED_ON_STARTUP=true \
    PORT=8000

EXPOSE 8000

# Run as a non-root user; the data directory must stay writable for SQLite.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health').status==200 else 1)"

# Single worker on purpose: SQLite is a single-writer database, and the
# reporting endpoints are read-heavy under WAL. Scaling past one process
# is the point at which you move to Postgres, not the point at which you
# add workers.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

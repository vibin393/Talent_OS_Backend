# Epic 13 (AD-37): Python 3.13 runtime. One image runs the API, the Celery worker and the Celery beat
# (the command is chosen in the root docker-compose.yml).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/data/hf-cache

# Build deps: psycopg2 (libpq), python3-saml / xmlsec (libxml2, libxmlsec1); curl for the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential pkg-config libpq-dev libxml2-dev libxmlsec1-dev libxmlsec1-openssl curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# API (default). Worker / beat commands are set in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

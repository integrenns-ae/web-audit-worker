# audit-worker: FastAPI-Plumbing + Regelwerk (Playwright-Crawl) als Subprozess
FROM python:3.12-slim

# libatomic1: Das von Playwright mitgelieferte node braucht libatomic.so.1.
# python:3.12-slim bringt sie nicht mit; ohne sie bricht `playwright install`
# mit exit 127 ab ("error while loading shared libraries").
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libatomic1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Worker- + Regelwerk-Abhängigkeiten
COPY requirements.txt /app/
COPY ruleset/requirements.txt /app/ruleset-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r ruleset-requirements.txt

# Chromium + OS-Libs für den Playwright-Crawl (der eigentliche Auditor)
RUN playwright install --with-deps chromium

# Regelwerk an einen SCHREIBBAREN Ort (nicht unter dem ro-Mount /app)
COPY ruleset /opt/ruleset

COPY . /app

ENV PYTHONUNBUFFERED=1 \
    AUDIT_DB_PATH=/data/audit.db \
    RULESET_DIR=/opt/ruleset \
    AUDIT_TIMEOUT_S=120
EXPOSE 8003
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003"]

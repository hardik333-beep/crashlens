# Worker image: arq background consumer. Shares the server package with the API.
# Build context is the repository root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY server/ /app/
RUN pip install --upgrade pip && pip install .

CMD ["arq", "app.worker.WorkerSettings"]

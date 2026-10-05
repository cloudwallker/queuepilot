FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QUEUEPILOT_DB=/app/data/queuepilot.db \
    QUEUEPILOT_HOST=0.0.0.0 \
    QUEUEPILOT_PORT=8765

WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY queuepilot ./queuepilot

RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    && groupadd --system queuepilot \
    && useradd --system --gid queuepilot --home-dir /app queuepilot \
    && mkdir -p /app/data \
    && chown queuepilot:queuepilot /app/data

USER queuepilot:queuepilot
EXPOSE 8765
CMD ["python", "-m", "queuepilot"]

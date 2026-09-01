# ─────────────────────────────────────────────────────────────
# Nexus QA │ Queue Metrics Exporter
# Lightweight container that exposes Redis Streams queue depth
# as Prometheus metrics for GPU engine auto-scaling (HPA).
# ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

RUN useradd -r -m -u 1000 nexus

WORKDIR /app

# Install minimal dependencies
COPY sdk/nexus-sdk/nexus_sdk/queue_metrics.py /app/nexus_sdk/queue_metrics.py
COPY sdk/nexus-sdk/nexus_sdk/__init__.py /app/nexus_sdk/__init__.py

RUN pip install --no-cache-dir prometheus-client redis

# Create a minimal __main__.py for module invocation
RUN echo 'from nexus_sdk.queue_metrics import main; main()' > /app/nexus_sdk/__main__.py

ENV PYTHONPATH=/app
ENV METRICS_PORT=9191
EXPOSE 9191

USER nexus
CMD ["python", "-m", "nexus_sdk"]

# ─────────────────────────────────────────────────────────────
# Nexus QA │ Orchestrator — workflow chain execution engine
# Production container — built on shared CPU base
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-base:latest
FROM ${BASE_IMAGE}

USER root

# Service-specific dependencies (SDK pre-installed in base image)
COPY requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt 2>/dev/null || true \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

# Application source (entrypoint is app.main, not a root main.py)
COPY app/ /app/service/app/
WORKDIR /app/service
RUN mkdir -p /data/nexus/orchestrator/uploads && chown -R nexus:nexus /data/nexus

# Runtime
ENV ENGINE_PORT=8100
EXPOSE 8100
USER nexus

CMD ["python", "-m", "app.main"]

# ─────────────────────────────────────────────────────────────
# Nexus QA │ Platform API — cross-cutting backend for all UI modules
# Production container — built on shared CPU base
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-base:latest
FROM ${BASE_IMAGE}

USER root

# Service-specific dependencies (SDK pre-installed in base image)
COPY requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

# Application source
COPY app/ /app/service/app/
COPY routers/ /app/service/routers/
COPY cache.py /app/service/cache.py
COPY main.py /app/service/main.py
WORKDIR /app/service

# Runtime
ENV ENGINE_PORT=8091
EXPOSE 8091
USER nexus

CMD ["python", "main.py"]

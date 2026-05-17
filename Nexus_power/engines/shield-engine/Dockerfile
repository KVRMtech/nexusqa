# ─────────────────────────────────────────────────────────────
# Nexus QA │ Shield Engine — PII redaction & compliance
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
COPY main.py /app/service/main.py
WORKDIR /app/service

# Shield key storage directory (writable by nexus user)
RUN mkdir -p /data/shield && chown nexus:nexus /data/shield

# Runtime
ENV ENGINE_PORT=8001
EXPOSE 8001
USER nexus

CMD ["python", "main.py"]

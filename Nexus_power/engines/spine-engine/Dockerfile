# ─────────────────────────────────────────────────────────────
# Nexus QA │ Spine Engine — workflow orchestration
# Production container — built on shared CPU base
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-base:latest
FROM ${BASE_IMAGE}

USER root

# ffprobe is needed for media probing (detecting audio codecs, duration, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ffprobe -version

# Service-specific dependencies (SDK pre-installed in base image).
# Reinstall SDK from context so Spine matches repo nexus-sdk (same as Eyes).
COPY engines/spine-engine/requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

COPY sdk/nexus-sdk/ /tmp/nexus-sdk/
RUN pip install --no-cache-dir --force-reinstall /tmp/nexus-sdk/ \
    && rm -rf /tmp/nexus-sdk

# Application source (compose build context = repo root)
COPY engines/spine-engine/app/ /app/service/app/
COPY engines/spine-engine/main.py /app/service/main.py
WORKDIR /app/service
RUN mkdir -p /data/nexus/documents /data/nexus/orchestrator/uploads && chown -R nexus:nexus /data/nexus

# Runtime
ENV ENGINE_PORT=8009
EXPOSE 8009
USER nexus

CMD ["python", "main.py"]

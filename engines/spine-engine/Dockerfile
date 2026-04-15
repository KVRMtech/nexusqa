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

# Service-specific dependencies (SDK pre-installed in base image)
COPY requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

# Application source
COPY app/ /app/service/app/
COPY main.py /app/service/main.py
WORKDIR /app/service
RUN mkdir -p /data/nexus/documents /data/nexus/orchestrator/uploads && chown -R nexus:nexus /data/nexus

# Runtime
ENV ENGINE_PORT=8009
EXPOSE 8009
USER nexus

CMD ["python", "main.py"]

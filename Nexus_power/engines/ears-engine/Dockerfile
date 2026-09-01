# ─────────────────────────────────────────────────────────────
# Nexus QA │ Ears Engine — audio transcription (GPU)
# Production container — built on shared GPU/CUDA base
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-gpu:latest
FROM ${BASE_IMAGE}

USER root

# ffmpeg is needed to extract audio from video files
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version

# Service-specific dependencies (SDK pre-installed in base image)
COPY requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

# Application source
COPY app/ /app/service/app/
COPY scripts/ /app/service/scripts/
COPY main.py /app/service/main.py
WORKDIR /app/service
RUN mkdir -p /app/service/data/audio && chown -R nexus:nexus /app/service/data

# Model directories: create writable dirs for Whisper + Pyannote model downloads
RUN mkdir -p /app/service/models/whisper-large-v3 /app/service/models/pyannote-speaker-3.1 \
    && chown -R nexus:nexus /app/service/models

# Runtime
ENV ENGINE_PORT=8002
ENV WHISPER_DEVICE=cuda
ENV WHISPER_COMPUTE_TYPE=float16
EXPOSE 8002
USER nexus

CMD ["python", "main.py"]

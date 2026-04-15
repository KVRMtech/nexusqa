# ─────────────────────────────────────────────────────────────
# Nexus QA │ Eyes Engine — screen/video analysis (GPU)
# Production container — built on shared GPU/CUDA base
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-gpu:latest
FROM ${BASE_IMAGE}

USER root

# ffmpeg is needed for video splitting (included in GPU base but not CPU base)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version && ffprobe -version

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
RUN mkdir -p /app/service/data/frames && chown -R nexus:nexus /app/service/data

# EasyOCR model directory: create, download models at build time, set ownership
RUN mkdir -p /app/service/models/easyocr \
    && python -c "import easyocr; easyocr.Reader(['en'], gpu=False, model_storage_directory='/app/service/models/easyocr')" \
    && chown -R nexus:nexus /app/service/models

# Runtime
ENV ENGINE_PORT=8003
ENV VISION_DEVICE=cuda
EXPOSE 8003
USER nexus

CMD ["python", "main.py"]

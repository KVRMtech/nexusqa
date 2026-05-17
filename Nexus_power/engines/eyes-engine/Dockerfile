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

# Service-specific dependencies (SDK pre-installed in base image).
# The `-e ../../sdk/nexus-sdk` line in requirements is stripped — always
# reinstall from the build context so Eyes picks up SDK changes without
# relying on a no-cache base rebuild.
COPY engines/eyes-engine/requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

COPY sdk/nexus-sdk/ /tmp/nexus-sdk/
RUN pip install --no-cache-dir --force-reinstall /tmp/nexus-sdk/ \
    && rm -rf /tmp/nexus-sdk

# Application source (compose build context = repo root)
COPY engines/eyes-engine/app/ /app/service/app/
COPY engines/eyes-engine/scripts/ /app/service/scripts/
COPY engines/eyes-engine/main.py /app/service/main.py
WORKDIR /app/service
RUN mkdir -p /app/service/data/frames && chown -R nexus:nexus /app/service/data

# EasyOCR model directory.
#
# Models are persisted in the `model-cache` named volume at runtime (mounted
# at /app/service/models in docker-compose), so the build-time prefetch is a
# COLD-START OPTIMISATION only — not a correctness requirement.  Wrap the
# download in a 180 s shell timeout + `|| true` so a slow / blocked CDN
# never wedges the entire image build (which is what happens when the
# upstream model mirror stalls).  If the download is skipped, EasyOCR will
# pull models on first request inside the container, which works because
# EYES_OCR_ALLOW_REMOTE_MODEL_BOOTSTRAP defaults to true.
ARG EYES_PREFETCH_OCR=true
RUN mkdir -p /app/service/models/easyocr \
    && if [ "$EYES_PREFETCH_OCR" = "true" ]; then \
        timeout 180 python -c "import easyocr; easyocr.Reader(['en'], gpu=False, model_storage_directory='/app/service/models/easyocr')" \
            || echo "[eyes-build] OCR model prefetch skipped (timeout/CDN issue) — runtime bootstrap will populate the volume on first request" ; \
       else \
        echo "[eyes-build] OCR model prefetch disabled via EYES_PREFETCH_OCR=$EYES_PREFETCH_OCR" ; \
       fi \
    && chown -R nexus:nexus /app/service/models

# Runtime
ENV ENGINE_PORT=8003
ENV VISION_DEVICE=cuda
EXPOSE 8003
USER nexus

CMD ["python", "main.py"]

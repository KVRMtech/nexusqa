# ─────────────────────────────────────────────────────────────
# Nexus QA │ Heart Engine — LLM inference (GPU)
# Production container — built on shared GPU/CUDA base
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-gpu:latest
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

# Runtime
ENV ENGINE_PORT=8004
ENV LLM_DEVICE=cuda
EXPOSE 8004
USER nexus

CMD ["python", "main.py"]

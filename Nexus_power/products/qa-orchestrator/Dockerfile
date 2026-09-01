# ─────────────────────────────────────────────────────────────
# Nexus QA │ QA Orchestrator — end-to-end QA pipeline
# Production container — built on shared CPU base
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-base:latest
FROM ${BASE_IMAGE}

USER root

# Application source
COPY app/ /app/service/app/
COPY main.py /app/service/main.py
WORKDIR /app/service

# Runtime
ENV ENGINE_PORT=8092
EXPOSE 8092
USER nexus

CMD ["python", "main.py"]

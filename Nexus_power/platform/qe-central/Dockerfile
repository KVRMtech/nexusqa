# ─────────────────────────────────────────────────────────────
# Nexus QA │ QE-Central — substrate writer + Phase-0 REFUSE harness
# Production container — built on shared CPU base (SDK preinstalled)
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-base:latest
FROM ${BASE_IMAGE}

USER root

# Service-specific dependencies (SDK pre-installed in base image)
COPY requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

# Application source + the qecentral alembic chain (run at deploy time:
#   alembic -c alembic_qec/alembic.ini upgrade head)
COPY app/ /app/service/app/
COPY alembic_qec/ /app/service/alembic_qec/
WORKDIR /app/service

# Working dirs: shared frame-storage mount point (NEXUS_FRAME_STORAGE_PATH)
# and the dev-only local KEK path. Created + chowned so startup mkdir
# succeeds even when the operator hasn't mounted volumes yet.
RUN mkdir -p /app/service/data/frames /app/service/data/kek \
    && chown -R nexus:nexus /app/service/data

# Runtime
ENV PORT=8093
EXPOSE 8093
USER nexus

CMD ["python", "-m", "app.main"]

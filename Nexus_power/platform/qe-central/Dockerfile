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
# Override the base image's healthcheck: it curls ${ENGINE_PORT:-8000}/health, but
# this service listens on PORT=8093 and does NOT set ENGINE_PORT — so the inherited
# probe hits port 8000, is refused, and the container is reported 'unhealthy' FOR
# EVER even while /health returns 200.  A permanent false-unhealthy is worse than
# none: it masks a REAL outage (nothing changes when the service actually dies) and
# can trip orchestrator restart/replace loops.  Probe the port this service binds.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8093}/health || exit 1
USER nexus

CMD ["python", "-m", "app.main"]

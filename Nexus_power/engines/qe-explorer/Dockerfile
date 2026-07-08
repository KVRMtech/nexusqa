# ─────────────────────────────────────────────────────────────
# Nexus QA │ QE-Central — Contained Explorer (S2), crawler-as-recorder
# Playwright-Python container that crawls client apps under a fail-closed
# guard. Design §3.2 pins this exact base image (same-version precedent:
# infrastructure/docker/Dockerfile.runner). It sits on internal-only networks
# and holds NO DB creds / NO KMS — only a per-fleet HMAC token (design §1.1).
# ─────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# The image already carries CPython 3.10 + Playwright 1.48.0 + browsers under
# /ms-playwright (world-readable), so a non-root user can drive them.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    QEC_EXPLORER_PORT=8210 \
    WORK_DIR=/work

# ── Service dependencies (pinned) ─────────────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# ── Application source + the versioned refuse pack ────────────
WORKDIR /app/service
COPY app/ /app/service/app/

# ── Non-root runtime + the shared crawl-storage mount point ───
# A dedicated least-privilege user owns the app + work dir. /work is the
# explorer-side mount of the shared qec-crawl-storage volume (manifests +
# staged PNGs land under /work/{crawl_id}/). Created + chowned so startup
# succeeds before the operator mounts the volume; the mount inherits ownership.
RUN groupadd --system explorer \
    && useradd --system --gid explorer --create-home --home-dir /home/explorer explorer \
    && mkdir -p "${WORK_DIR}" \
    && chown -R explorer:explorer "${WORK_DIR}" /app/service

# Isolation invariant: NO host port is published for this service (see
# docker-compose.qec.yml). qe-central reaches it only over the internal
# qec-internal network at qe-explorer:8210. `expose` documents the port for
# container-to-container use without opening a host-reachable path.
EXPOSE 8210
USER explorer

# app.main is authored by the explorer lifecycle stage; it binds
# 0.0.0.0:${QEC_EXPLORER_PORT}. Fail loudly if it is missing rather than
# silently idling.
CMD ["python", "-m", "app.main"]

# ─────────────────────────────────────────────────────────────
# Nexus QA │ repo-intel Engine (S3) — repository intelligence
# Production container — built on the shared CPU base (SDK preinstalled).
# NO Playwright (unlike legs/qe-explorer); adds git + tree-sitter wheels.
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-base:dev
FROM ${BASE_IMAGE}

USER root

# System dependency: git. The connector shells out to it (shallow clone
# depth=1 --filter=blob:none) and passes the access token via the
# environment (GIT_CONFIG_*), never via command-line args, so the token is
# not exposed in the process list.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Service-specific Python dependencies (SDK pre-installed in the base image;
# the -e SDK line is stripped so we don't reinstall it).
COPY requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

# Application source
COPY app/ /app/service/app/
COPY main.py /app/service/main.py
WORKDIR /app/service

# Per-tenant repo workdir root (REPO_INTEL_WORKDIR_ROOT) + dev-only local KEK
# path. Created + chowned so startup succeeds before any volume is mounted.
RUN mkdir -p /app/service/data/repos /app/service/data/kek \
    && chown -R nexus:nexus /app/service/data

# Runtime
ENV ENGINE_PORT=8014
EXPOSE 8014
USER nexus

CMD ["python", "main.py"]

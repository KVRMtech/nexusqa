# ─────────────────────────────────────────────────────────────
# Nexus QA │ Legs Engine — browser automation
# Production container — built on shared CPU base
# ─────────────────────────────────────────────────────────────
ARG BASE_IMAGE=nexus-base:latest
FROM ${BASE_IMAGE}

USER root

# Service-specific dependencies (SDK pre-installed in base image)
COPY requirements.txt /tmp/requirements.txt
RUN sed '/^-e/d' /tmp/requirements.txt > /tmp/svc.txt \
    && pip install --no-cache-dir -r /tmp/svc.txt \
    && rm -f /tmp/requirements.txt /tmp/svc.txt

# Playwright browsers — install to a shared, world-readable cache so the
# nexus user can launch them after USER drops privileges below. The
# default $HOME/.cache/ms-playwright path lands in /root/, which becomes
# unreachable post-USER-switch. PLAYWRIGHT_BROWSERS_PATH pins the
# location; chmod makes it readable + executable by any process.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir playwright \
    && mkdir -p /ms-playwright \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright \
    && find /ms-playwright -type d -exec chmod a+rwx {} \;

# Application source
COPY app/ /app/service/app/
COPY main.py /app/service/main.py
WORKDIR /app/service

# Working dirs for evidence/screenshots/traces. The engine's
# evidence_storage_path default is "./data/evidence" relative to
# WORKDIR; create + chown so on_startup mkdir succeeds even when the
# operator hasn't overridden EVIDENCE_STORAGE_PATH to a mounted volume.
RUN mkdir -p /app/service/data/evidence \
    && chown -R nexus:nexus /app/service/data

# Runtime
ENV ENGINE_PORT=8007
EXPOSE 8007
USER nexus

CMD ["python", "main.py"]

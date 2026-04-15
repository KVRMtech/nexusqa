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

# Playwright browsers (headless Chromium for automation)
RUN pip install --no-cache-dir playwright \
    && python -m playwright install --with-deps chromium

# Application source
COPY app/ /app/service/app/
COPY main.py /app/service/main.py
WORKDIR /app/service

# Runtime
ENV ENGINE_PORT=8007
EXPOSE 8007
USER nexus

CMD ["python", "main.py"]

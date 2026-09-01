# ─────────────────────────────────────────────────────────────
# Nexus QA │ Platform API — cross-cutting backend for all UI modules
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

# Application source
COPY app/ /app/service/app/
COPY routers/ /app/service/routers/
COPY cache.py /app/service/cache.py
COPY main.py /app/service/main.py
COPY scripts/ /app/service/scripts/
WORKDIR /app/service

# Runtime directories, owned by the user the service actually runs as.
#
# Neither of these existed in the image, and both are created at runtime — but the
# service runs as `nexus` while /app/service and /home/nexus are root-owned, so it
# could not create them. Two consequences, both seen in production:
#
#   * /app/service/data — the frame annotator failed with EACCES and silently
#     disabled itself; the log line is a warning nobody reads.
#   * ~/.nexus/kek     — LocalKekProvider GENERATES a fresh master key when the file
#     is absent. So a container recreate silently re-keyed the service and every
#     envelope blob written under the old key stopped decrypting
#     ("wrapped DEK failed authentication"), taking stored sessions and credential
#     cards with it. Creating the directory here is only half the fix; compose
#     mounts a VOLUME over it so the key survives the container.
RUN mkdir -p /app/service/data /home/nexus/.nexus/kek \
    && chown -R nexus:nexus /app/service/data /home/nexus/.nexus \
    && chmod 700 /home/nexus/.nexus/kek

ENV ENGINE_PORT=8091
EXPOSE 8091
USER nexus

CMD ["python", "main.py"]

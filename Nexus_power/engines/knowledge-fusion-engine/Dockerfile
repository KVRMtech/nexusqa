# Knowledge Fusion Engine — Phase 1 substrate builder.
#
# Multi-stage build:
#   * builder      — compile / cache dependencies
#   * runtime      — slim image with ffmpeg for clip service

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY requirements.txt /build/requirements.txt
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/sdk

# ffmpeg for the clip service. tini for proper signal handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app
COPY . /app

# The SDK lives outside the engine directory; docker-compose mounts it
# into /sdk at runtime. PYTHONPATH already includes /sdk.
EXPOSE 8020

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8020/health',timeout=3).getcode()==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py"]

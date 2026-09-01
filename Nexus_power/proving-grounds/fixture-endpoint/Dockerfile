# The two-endpoint environment fixture (I1).
#
# STDLIB ONLY, so there is no install step and nothing to go stale: a proving
# ground that needs a dependency tree is a proving ground that gets skipped.
FROM python:3.11-slim

WORKDIR /app
COPY server.py /app/server.py

# 0.0.0.0 because this runs in a container; the TOKEN is supplied at run time
# and is enforced when set. Unset means an open fixture, which is the local
# development shape and never the deployed one.
ENV FIXTURE_PORT=8130
ENV FIXTURE_TOKEN=""

EXPOSE 8130
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8130/healthz',timeout=2).status==200 else 1)"

CMD ["sh", "-c", "python /app/server.py --host 0.0.0.0 --port ${FIXTURE_PORT} --token \"${FIXTURE_TOKEN}\""]

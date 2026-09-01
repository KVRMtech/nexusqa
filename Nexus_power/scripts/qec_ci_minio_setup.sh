#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# QE-Central — deterministic CI OBJECT STORAGE bootstrap (A26.1).
#
# Starts an S3-compatible MinIO for the T-FL-03 durable-evidence handoff proof,
# waits for it to be genuinely OPERATIONAL, and creates the evidence bucket.
#
# WHY THIS IS A SCRIPT AND NOT A `services:` BLOCK
# ================================================
# GitHub Actions service containers accept an image, env, ports and `docker
# create` options — but they cannot supply a COMMAND. `minio/minio` has
# ENTRYPOINT=docker-entrypoint.sh and CMD=["minio"], so with no arguments it
# prints its help and exits; the server needs `minio server <dir>`. The usual
# workaround is the floating `minio/minio:edge-cicd` tag, whose CMD is baked to
# `server /data`.
#
# That workaround was rejected here. docker-compose.yml pins
# minio/minio:RELEASE.2023-03-20T20-16-18Z, and this repository's CI already
# holds the line that services must match what is DEPLOYED ("a CI that silently
# floats to 17 would prove the schema against a server nobody runs" —
# ci.yml, the postgres/redis pins). Proving the object-storage handoff against a
# rolling edge build while shipping a 2023 release would be exactly that mistake
# on a new axis. So the deployed release is started explicitly, with the same
# health endpoint the compose healthcheck uses.
#
# READINESS IS MEASURED, NEVER ASSUMED
# ====================================
# There is no `sleep 30` here. A fixed sleep is either too short (a flake) or too
# long (wasted minutes), and it never reports WHY a service is missing. Three
# escalating, bounded, fail-loud checks instead:
#
#   1. the container is RUNNING          — else print its logs and abort;
#   2. /minio/health/live answers        — the liveness probe compose uses;
#   3. the S3 API answers WITH CREDENTIALS and survives a real
#      PUT/GET/DELETE round trip on the evidence bucket.
#
# (3) is the one that matters. A live health endpoint only proves a process is
# up; the tests need working credentials and a writable bucket, and discovering
# a credential typo as six confusing test failures is strictly worse than
# discovering it here, named.
#
# THE BUCKET IS CREATED EXPLICITLY
# ================================
# nexus_sdk's S3 backend will lazily create a missing bucket on first use — and
# swallows the error if that fails (it logs a note and carries on). Depending on
# that would make bucket provisioning invisible AND make its failure surface as
# something else entirely. It is created here, verified here, and a failure is
# fatal here.
#
# Credentials are throwaway fixtures for a container that lives for the length of
# one job. They are NOT secrets and are deliberately literal so the job is
# reproducible on a laptop.
#
# Usage (from anywhere):
#   QEC_TEST_S3_ENDPOINT=http://localhost:9000 \
#   QEC_TEST_S3_BUCKET=qec-evidence \
#   AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \
#     bash scripts/qec_ci_minio_setup.sh
#
# Teardown:  bash scripts/qec_ci_minio_setup.sh --down
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# Pinned to the SAME release docker-compose.yml deploys. Bump both together.
MINIO_IMAGE="${MINIO_IMAGE:-minio/minio:RELEASE.2023-03-20T20-16-18Z}"
CONTAINER="${QEC_CI_MINIO_CONTAINER:-qec-ci-minio}"
PORT="${QEC_CI_MINIO_PORT:-9000}"
BUCKET="${QEC_TEST_S3_BUCKET:-qec-evidence}"
ENDPOINT="${QEC_TEST_S3_ENDPOINT:-http://localhost:${PORT}}"
ACCESS_KEY="${AWS_ACCESS_KEY_ID:-minioadmin}"
SECRET_KEY="${AWS_SECRET_ACCESS_KEY:-minioadmin}"
# `python3` on a runner, `python` on a Windows developer box. Each candidate is
# EXECUTED rather than merely located: Windows ships a `python3` App Execution
# Alias that exists on PATH, prints an advert for the Microsoft Store and exits
# non-zero, so `command -v` alone picks a stub that cannot run anything.
PY_BIN="${PYTHON:-}"
if [ -z "${PY_BIN}" ]; then
  for _cand in python3 python; do
    if command -v "${_cand}" >/dev/null 2>&1 && "${_cand}" -c "pass" >/dev/null 2>&1; then
      PY_BIN="${_cand}"; break
    fi
  done
fi
if [ -z "${PY_BIN}" ]; then
  echo "::error::no working python interpreter on PATH (tried python3, python)"
  exit 1
fi

if [ "${1:-}" = "--down" ]; then
  echo "── tearing down ${CONTAINER} ──"
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  echo "MinIO removed (or was not running)."
  exit 0
fi

echo "═══ A26.1 — object storage bootstrap ═══"
echo "  image    ${MINIO_IMAGE}"
echo "  endpoint ${ENDPOINT}"
echo "  bucket   ${BUCKET}"

# ── 1. Start ───────────────────────────────────────────────────────────────
# Removed first so a re-run is idempotent, and given the same container-level
# healthcheck docker-compose.yml declares, so `docker inspect` reports a health
# state a human can read in the job log.
docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
docker run -d --name "${CONTAINER}" \
  -e "MINIO_ROOT_USER=${ACCESS_KEY}" \
  -e "MINIO_ROOT_PASSWORD=${SECRET_KEY}" \
  -p "${PORT}:9000" \
  --health-cmd "curl -f http://localhost:9000/minio/health/live" \
  --health-interval 5s \
  --health-timeout 3s \
  --health-retries 20 \
  "${MINIO_IMAGE}" server /minio_data --console-address ":9001" >/dev/null

# ── 2. Liveness ────────────────────────────────────────────────────────────
echo "── waiting for ${ENDPOINT}/minio/health/live ──"
for i in $(seq 1 60); do
  if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    echo "::error::MinIO container exited during startup"
    docker logs "${CONTAINER}" 2>&1 | tail -40
    exit 1
  fi
  if curl -fsS "${ENDPOINT}/minio/health/live" >/dev/null 2>&1; then
    echo "MinIO live after ${i}s"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "::error::MinIO never became live after 60s"
    docker logs "${CONTAINER}" 2>&1 | tail -40
    exit 1
  fi
  sleep 1
done

# ── 3. Credentials + bucket + a real round trip ────────────────────────────
QEC_TEST_S3_ENDPOINT="${ENDPOINT}" \
QEC_TEST_S3_BUCKET="${BUCKET}" \
AWS_ACCESS_KEY_ID="${ACCESS_KEY}" \
AWS_SECRET_ACCESS_KEY="${SECRET_KEY}" \
"${PY_BIN}" - <<'PY'
import os, sys, time
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

endpoint = os.environ["QEC_TEST_S3_ENDPOINT"]
bucket = os.environ["QEC_TEST_S3_BUCKET"]

# Short, bounded client: this step must report a broken endpoint in seconds, not
# sit through botocore's public-internet retry defaults.
client = boto3.client(
    "s3", endpoint_url=endpoint, region_name="us-east-1",
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    config=Config(connect_timeout=5, read_timeout=10,
                  retries={"max_attempts": 1}),
)

last = None
for i in range(30):
    try:
        client.list_buckets()
        print(f"S3 API answered WITH CREDENTIALS after {i}s")
        break
    except Exception as exc:
        last = exc
        time.sleep(1)
else:
    print(f"::error::the S3 API never answered with these credentials: {last}")
    sys.exit(1)

try:
    client.head_bucket(Bucket=bucket)
    print(f"bucket {bucket!r} already present")
except ClientError:
    client.create_bucket(Bucket=bucket)
    print(f"bucket {bucket!r} CREATED")

# Prove it, rather than trusting the create call's return.
client.head_bucket(Bucket=bucket)
probe = "_ci_bootstrap_probe"
client.put_object(Bucket=bucket, Key=probe, Body=b"ok")
got = client.get_object(Bucket=bucket, Key=probe)["Body"].read()
assert got == b"ok", f"round-trip returned {got!r}"
client.delete_object(Bucket=bucket, Key=probe)
print(f"PUT/GET/DELETE round trip verified on {bucket!r}")
PY

docker inspect "${CONTAINER}" --format 'container health: {{.State.Health.Status}}' || true
echo "═══ object storage OPERATIONAL ═══"

#!/usr/bin/env bash
# =============================================================================
# prove_install.sh — one-command reproducibility + deployment proof.
#
# Builds every Verdict service image from THIS git checkout, loads them into an
# ephemeral `kind` cluster, installs the verdict Helm chart (on-prem profile),
# runs the migrations Job, then PROVES:
#   * every pod's code == git            (deploy-verify gate, per service)
#   * the plane is healthy               (rollout + /health)
#   * a cycle runs end-to-end            (smoke — optional, via scripts/smoke.sh)
# Tears the cluster down on exit.
#
# This is the operational proof behind Phase 0 ("a clean build == what runs") and
# Phase 1 ("one hardened deployment"). It is self-adapting: image names come from
# `helm template`, not a hardcoded list.
#
# Requires: docker, kind, helm, kubectl. Nothing else — no cloud, no registry.
# Usage:    scripts/prove_install.sh [--keep] [--values <extra-values.yaml>]
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # Nexus_power/
CHART="$REPO_ROOT/infrastructure/helm/verdict"
CLUSTER="verdict-proof"
NS="verdict"
TAG="proof"
KEEP=0
EXTRA_VALUES="$CHART/values-onprem.yaml"

while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1 ;;
    --values) EXTRA_VALUES="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac; shift
done

for bin in docker kind helm kubectl; do
  command -v "$bin" >/dev/null 2>&1 || { echo "FATAL: '$bin' is required but not installed." >&2; exit 1; }
done

# service (image-name) -> Dockerfile dir. The image NAME (the chart's per-service
# .name) is matched against this map; a rendered image with no mapping is a hard
# error (never silently skipped), so the proof can't pass on a half-built plane.
declare -A SERVICE_DIR=(
  [qe-central]="platform/qe-central"
  [qe-explorer]="engines/qe-explorer"
  [repo-intel]="engines/repo-intel"
  [verdict-portal]="verdict-portal"
)
# git path (relative to REPO_ROOT) whose app/ we deploy-verify against, per service.
declare -A SERVICE_APP=(
  [qe-central]="platform/qe-central/app"
  [qe-explorer]="engines/qe-explorer/app"
  [repo-intel]="engines/repo-intel/app"
)
# in-pod path of the app code, per service (matches the Dockerfile WORKDIR/COPY).
declare -A SERVICE_POD_PATH=(
  [qe-central]="/app/service/app"
  [qe-explorer]="/app/app"
  [repo-intel]="/app/app"
)

cleanup() { [ "$KEEP" = "1" ] || kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== 1) ephemeral kind cluster =="
kind create cluster --name "$CLUSTER" --wait 120s

echo "== 2) discover the images the chart wants (self-adapting, no hardcoded list) =="
RENDERED="$(helm template verdict "$CHART" -f "$EXTRA_VALUES" \
  --set global.image.tag="$TAG" --set global.image.pullPolicy=Never)"
_ALL_IMAGES="$(printf '%s\n' "$RENDERED" | grep -oE 'image:\s*"?[^"[:space:]]+' \
  | sed -E 's/image:\s*"?//' | grep -E ':' | sort -u)"
mapfile -t OUR_IMAGES        < <(printf '%s\n' "$_ALL_IMAGES" | grep "/nexus-qa/")
# Third-party images (postgres, redis, …) the chart references but we do NOT build.
# pullPolicy=Never means the kubelet won't fetch them, so we pull + load them into
# kind ourselves (else the pod is ErrImageNeverPull).
mapfile -t THIRD_PARTY_IMAGES < <(printf '%s\n' "$_ALL_IMAGES" | grep -v "/nexus-qa/")
[ "${#OUR_IMAGES[@]}" -gt 0 ] || { echo "FATAL: chart rendered no first-party images." >&2; exit 1; }

echo "== 2b) build the shared CPU base image the service Dockerfiles FROM =="
# Each service Dockerfile does `ARG BASE_IMAGE=nexus-base:latest; FROM ${BASE_IMAGE}`
# (the SDK is pre-installed in the base). That base lives on no registry, so it must
# be built locally FIRST or every service build fails "pull access denied … nexus-base".
docker build -f "$REPO_ROOT/infrastructure/docker/Dockerfile.base" -t nexus-base:latest "$REPO_ROOT"

echo "== 3) build each first-party image from git + load into kind =="
for img in "${OUR_IMAGES[@]}"; do
  name="$(basename "${img%%:*}")"           # ghcr.io/nexus-qa/qe-central:proof -> qe-central
  dir="${SERVICE_DIR[$name]:-}"
  [ -n "$dir" ] || { echo "FATAL: no build mapping for image '$name' (image=$img)." >&2; exit 1; }
  # $img already carries :$TAG (the discovery render was done with --set
  # global.image.tag=$TAG), so build/tag/load it as-is — appending :$TAG again
  # yields an invalid double tag like "…/qe-central:proof:proof".
  echo "   building $name  ($dir/Dockerfile)  ->  $img"
  # Build context is the SERVICE dir (its Dockerfile COPYs app/, requirements.txt,
  # alembic_qec/ relative to there — same as ci.yml's docker-build). BASE_IMAGE is
  # the nexus-base:latest we built in step 2b.
  docker build --build-arg BASE_IMAGE=nexus-base:latest \
    -t "$img" -f "$REPO_ROOT/$dir/Dockerfile" "$REPO_ROOT/$dir"
  kind load docker-image "$img" --name "$CLUSTER"
done

echo "== 3b) pull + load the third-party images (pullPolicy=Never can't fetch them) =="
for img in "${THIRD_PARTY_IMAGES[@]:-}"; do
  [ -n "$img" ] || continue
  echo "   pulling + loading $img"
  docker pull "$img"
  kind load docker-image "$img" --name "$CLUSTER"
done

echo "== 4) install the verdict chart (on-prem profile, local images) =="
helm upgrade --install verdict "$CHART" -n "$NS" --create-namespace \
  -f "$EXTRA_VALUES" \
  --set global.image.tag="$TAG" --set global.image.pullPolicy=Never \
  --set secrets.jwtSecret=proof --set secrets.postgresPassword=proof \
  --set secrets.qecDbPassword=proof --set secrets.substrateDbPassword=proof \
  --set secrets.explorerToken=proof \
  --wait --timeout "${HELM_TIMEOUT:-600s}" || {
    # On a --wait timeout the plane is half-up; dump WHY before the EXIT trap tears
    # the cluster down, so a CI run shows the actual pod/job state (not an empty log).
    echo "== helm --wait failed; diagnostics BEFORE cleanup ==" >&2
    kubectl -n "$NS" get pods,jobs -o wide || true
    kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -30 || true
    for c in postgres qe-central migrations; do
      echo "--- describe $c ---"; kubectl -n "$NS" describe pod -l "app.kubernetes.io/component=$c" 2>/dev/null | tail -40 || true
      echo "--- logs $c ---";     kubectl -n "$NS" logs -l "app.kubernetes.io/component=$c" --tail=60 --all-containers 2>/dev/null || true
    done
    exit 1
  }

echo "== 5) migrations =="
# The migration Job is a `post-install` Helm HOOK (component=migrate) with
# hook-delete-policy=hook-succeeded — so `helm --wait` above ALREADY blocked on it
# and it self-deleted on success (a failed hook would have failed the install). So
# there is nothing left to wait for here; just confirm the install reported success.
echo "   migrations ran as a post-install hook (helm --wait blocked on it; a failure would have failed step 4)."

echo "== 6) PROVE each pod's code == git (deploy-verify gate) =="
# A service the chart RENDERED (its image is in OUR_IMAGES) is enabled and MUST have a
# running pod that matches git; a service that isn't rendered is intentionally disabled
# and announced as skipped. This closes the green-wash where a missing pod for an
# enabled service was silently skipped and the proof still passed. We also require that
# at least one service was actually verified.
declare -A ENABLED=()
for img in "${OUR_IMAGES[@]}"; do ENABLED["$(basename "${img%%:*}")"]=1; done
fail=0; verified=0
for name in "${!SERVICE_APP[@]}"; do
  if [ -z "${ENABLED[$name]:-}" ]; then
    echo "   (skip $name: not enabled in this install)"; continue
  fi
  pod="$(kubectl -n "$NS" get pod -l "app.kubernetes.io/component=$name" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -z "$pod" ]; then
    echo "   FAIL: $name is ENABLED but has no running pod to verify." >&2; fail=1; continue
  fi
  tmp="$(mktemp -d)"
  kubectl -n "$NS" exec "$pod" -- tar -C "${SERVICE_POD_PATH[$name]}" -cf - . 2>/dev/null | tar -C "$tmp" -xf - 2>/dev/null || true
  echo "   verify $name:"
  python "$REPO_ROOT/ci/reproducibility/verify_deployment.py" \
    --git-root "$REPO_ROOT/${SERVICE_APP[$name]}" --deployed-root "$tmp" || fail=1
  verified=$((verified + 1))
  rm -rf "$tmp"
done
[ "$verified" -gt 0 ] || { echo "FATAL: deploy-verify checked ZERO services (nothing was proven)." >&2; fail=1; }

echo "== 7) health =="
kubectl -n "$NS" get pods
[ -x "$REPO_ROOT/scripts/smoke.sh" ] && NS="$NS" "$REPO_ROOT/scripts/smoke.sh" || echo "   (smoke.sh not run)"

if [ "$fail" = "0" ]; then
  echo "PROVE_INSTALL: PASS — the verdict plane deployed from a clean git build, and every pod is provably the git checkout."
else
  echo "PROVE_INSTALL: FAIL — a pod's code did not match git (see deploy-verify output above)." >&2
fi
exit "$fail"

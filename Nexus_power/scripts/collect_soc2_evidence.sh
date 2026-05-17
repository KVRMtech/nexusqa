#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# SOC2 monthly evidence collection (Phase 15)
# ─────────────────────────────────────────────────────────────────────
# Run on the 1st of every month against the production cluster. Outputs
# a dated directory under compliance/evidence/ that the auditor will
# pull from during the engagement.
#
# Usage:
#   KUBE_NAMESPACE=nexus-platform bash scripts/collect_soc2_evidence.sh
#
# Required:
#   - kubectl with read access to the production namespace
#   - Postgres read access (or a kubectl exec capability into the pg pod)
#   - gh CLI authenticated (for change-log pull)
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

NAMESPACE="${KUBE_NAMESPACE:-nexus-platform}"
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
MONTH=$(date +%Y-%m)
OUT="${REPO_ROOT}/compliance/evidence/${MONTH}"
mkdir -p "${OUT}"

c_green() { printf '\033[32m%s\033[0m' "$*"; }
c_red()   { printf '\033[31m%s\033[0m' "$*"; }
step() { echo; echo "==> $*"; }
ok()   { echo "    $(c_green '✓') $*"; }
warn() { echo "    $(c_red '⚠') $*"; }

# ─── 1. RBAC snapshot ───────────────────────────────────────────────
step "1/6  RBAC snapshot"
{
  echo "# K8s RBAC — ${MONTH}"
  echo "# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  kubectl get clusterrolebindings -o yaml
  echo "---"
  kubectl get rolebindings -n "${NAMESPACE}" -o yaml
  echo "---"
  kubectl get serviceaccounts -n "${NAMESPACE}" -o yaml
} > "${OUT}/rbac_snapshot.yaml" 2>&1 || warn "kubectl access denied — populate manually"
ok "rbac_snapshot.yaml"

# ─── 2. Access logs (last 30 days) ──────────────────────────────────
step "2/6  Access logs (last 30 days)"
mkdir -p "${OUT}/access_logs"
PG_POD=$(kubectl get pods -n "${NAMESPACE}" -l app.kubernetes.io/component=postgres \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -n "${PG_POD}" ]; then
  kubectl exec -n "${NAMESPACE}" "${PG_POD}" -- \
    psql -U nexus -d nexus -t -c "
      COPY (
        SELECT created_at, actor_user_id, actor_tenant_id, action, resource, status
        FROM audit_log
        WHERE created_at > now() - interval '30 days'
        ORDER BY created_at DESC
      ) TO STDOUT WITH CSV HEADER
    " > "${OUT}/access_logs/audit_log_30d.csv" 2>&1 || warn "audit_log query failed"
  ok "audit_log_30d.csv"
else
  warn "no Postgres pod found — populate audit_log_30d.csv manually"
fi

# ─── 3. Dependency versions + CVE scan ─────────────────────────────
step "3/6  Dependency versions + CVE scan"
{
  echo "# Python deps (top-level)"
  for r in "${REPO_ROOT}"/engines/*/requirements.txt \
           "${REPO_ROOT}"/sdk/nexus-sdk/setup.py \
           "${REPO_ROOT}"/platform/*/requirements.txt; do
    [ -f "$r" ] || continue
    echo
    echo "## ${r#${REPO_ROOT}/}"
    cat "$r"
  done
} > "${OUT}/dependency_versions.txt"
ok "dependency_versions.txt"

if command -v pip-audit >/dev/null 2>&1; then
  pip-audit --format json > "${OUT}/cve_scan.json" 2>&1 || warn "pip-audit failed"
  ok "cve_scan.json"
else
  warn "pip-audit not installed — install with: pip install pip-audit"
fi

if command -v trivy >/dev/null 2>&1; then
  IMAGES=$(kubectl get pods -n "${NAMESPACE}" \
    -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' \
    2>/dev/null | sort -u)
  : > "${OUT}/container_cves.txt"
  for img in $IMAGES; do
    echo "==> $img" >> "${OUT}/container_cves.txt"
    trivy image --quiet --severity HIGH,CRITICAL "$img" >> "${OUT}/container_cves.txt" 2>&1 || true
  done
  ok "container_cves.txt"
else
  warn "trivy not installed — install for container CVE scan"
fi

# ─── 4. DR drill history ────────────────────────────────────────────
step "4/6  DR drill history"
if [ -d "${REPO_ROOT}/docs/drill_reports" ]; then
  cp -r "${REPO_ROOT}/docs/drill_reports" "${OUT}/dr_drill_reports"
  ok "dr_drill_reports/"
else
  warn "docs/drill_reports/ missing — operator must run dr_drills.sh"
fi

# ─── 5. Change log (last 30 days of PRs) ───────────────────────────
step "5/6  Change log (last 30 days)"
if command -v gh >/dev/null 2>&1; then
  gh pr list --state merged --limit 200 \
    --json number,title,author,mergedAt,reviewDecision,mergeCommit \
    --jq '.[] | select(.mergedAt > (now - 30*86400 | todate))' \
    > "${OUT}/merged_prs_30d.json" 2>&1 || warn "gh pr list failed"
  ok "merged_prs_30d.json"
else
  warn "gh CLI not installed — install with: brew install gh / scoop install gh"
fi

# ─── 6. Configuration drift detection ──────────────────────────────
step "6/6  Configuration drift"
{
  echo "# Helm-rendered manifest snapshot — ${MONTH}"
  echo
  echo "## values-production.yaml at HEAD"
  cat "${REPO_ROOT}/infrastructure/helm/nexus-qa/values-production.yaml" 2>/dev/null || echo "(missing)"
} > "${OUT}/helm_values_snapshot.yaml"
ok "helm_values_snapshot.yaml"

# Compare against last month's snapshot to detect drift.
LAST_MONTH=$(date -d "$(date +%Y-%m-01) -1 month" +%Y-%m 2>/dev/null || \
             date -v-1m +%Y-%m 2>/dev/null || echo "")
if [ -n "${LAST_MONTH}" ] && [ -f "${REPO_ROOT}/compliance/evidence/${LAST_MONTH}/helm_values_snapshot.yaml" ]; then
  diff -u \
    "${REPO_ROOT}/compliance/evidence/${LAST_MONTH}/helm_values_snapshot.yaml" \
    "${OUT}/helm_values_snapshot.yaml" \
    > "${OUT}/config_drift_vs_${LAST_MONTH}.diff" || true
  ok "config_drift_vs_${LAST_MONTH}.diff"
fi

# ─── Summary ────────────────────────────────────────────────────────
echo
ok "Evidence collected at: ${OUT}"
echo
echo "Manifest:"
ls -la "${OUT}"
echo
echo "Next: review the warnings above. Each warned item needs a"
echo "manual collection step before the auditor reviews this folder."

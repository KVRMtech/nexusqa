#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# A31 — KEDA on a REAL cluster: apply, observe, and record a queue-driven
#       scaling event.
#
# WHY A SCRIPT AND NOT A CHECKLIST
# ================================
# A31's acceptance is "the evidence must include the actual cluster
# observation". A transcript of someone typing kubectl is not reproducible, and
# a YAML lint pass is explicitly not sufficient. So the whole proof — cluster,
# operator, manifests, the scale event and its evidence — is one command that
# either exits 0 having observed a real scale-up, or fails loudly.
#
# WHAT IT PROVES, IN ORDER
#   1. the cluster exists and its node is Ready
#   2. the KEDA operator is installed and its CRDs are served
#   3. the REPOSITORY'S OWN ScaledObject applies UNEDITED
#   4. KEDA resolves the trigger and reaches Prometheus (READY=True)
#   5. at queue depth 0 the fleet sits at minReplicaCount
#   6. RAISING QUEUE DEPTH CAUSES KEDA TO SCALE THE DEPLOYMENT OUT
#   7. the new pods are SCHEDULED and RUNNING on a node (named in the evidence)
#   8. dropping the depth is recorded too, so scale-down behaviour is visible
#
# THE NEGATIVE CONTROL
# ====================
# Step 5 is not decoration. If the deployment were already at 4 replicas for an
# unrelated reason, step 6 would "pass" without KEDA having done anything. The
# run therefore asserts the fleet is at exactly minReplicaCount BEFORE the queue
# is raised, so the observed change can only be the scaler's doing.
#
# Usage:  bash scripts/gate4_a31_keda_cluster_proof.sh [--keep] [--recreate]
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

CLUSTER="${GATE4_CLUSTER:-gate4-keda}"
CTX="kind-${CLUSTER}"
NODE_IMAGE="${GATE4_NODE_IMAGE:-kindest/node:v1.32.2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAFFOLD="$HERE/infrastructure/keda/gate4-proof/00-fleet-under-test.yaml"
PRODUCTION_MANIFEST="$HERE/infrastructure/keda/qe-explorer-scaledobject.yaml"
EVIDENCE_DIR="${GATE4_EVIDENCE_DIR:-$HERE/evidence/gate4}"
EVIDENCE="$EVIDENCE_DIR/a31_keda_cluster.json"
RECREATE=0
KEEP=0
for a in "$@"; do
  case "$a" in
    --recreate) RECREATE=1 ;;
    --keep) KEEP=1 ;;
  esac
done

k() { kubectl --context "$CTX" "$@"; }
say() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

mkdir -p "$EVIDENCE_DIR"
[ -f "$PRODUCTION_MANIFEST" ] || die "production manifest missing: $PRODUCTION_MANIFEST"
[ -f "$SCAFFOLD" ] || die "scaffold missing: $SCAFFOLD"

# ── 1. Cluster ─────────────────────────────────────────────────────────────
say "1. cluster"
if [ "$RECREATE" = "1" ] || ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  MSYS_NO_PATHCONV=1 kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  MSYS_NO_PATHCONV=1 kind create cluster --name "$CLUSTER" --image "$NODE_IMAGE" --wait 300s \
    2>&1 | grep -vE "round_trippers|envvar\.go|loader\.go" | tail -5
fi
k wait --for=condition=Ready node --all --timeout=180s >/dev/null || die "node never became Ready"
NODE_JSON="$(k get nodes -o json | python -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps([{'name':n['metadata']['name'],
                   'kubelet':n['status']['nodeInfo']['kubeletVersion'],
                   'runtime':n['status']['nodeInfo']['containerRuntimeVersion'],
                   'os':n['status']['nodeInfo']['osImage']} for n in d['items']]))")"
echo "   node: $NODE_JSON"

# CoreDNS must be healthy or every trigger query fails on DNS, which reads as a
# scaler bug. (An abrupt Docker kill corrupts containerd snapshots and leaves
# CoreDNS in CreateContainerError — observed once during this milestone.)
k -n kube-system wait --for=condition=Ready pod -l k8s-app=kube-dns --timeout=180s >/dev/null \
  || die "CoreDNS is not Ready — cluster DNS is broken, recreate with --recreate"

# ── 2. KEDA ────────────────────────────────────────────────────────────────
say "2. KEDA operator"
if ! helm status keda -n keda --kube-context "$CTX" >/dev/null 2>&1; then
  helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
  helm repo update kedacore >/dev/null 2>&1 || true
  helm install keda kedacore/keda -n keda --create-namespace --kube-context "$CTX" \
    --timeout 10m 2>&1 | tail -2 || true
fi
k -n keda wait --for=condition=Available deploy --all --timeout=420s >/dev/null \
  || die "KEDA deployments never became Available"
KEDA_VERSION="$(k -n keda get deploy keda-operator -o jsonpath='{.spec.template.spec.containers[0].image}')"
echo "   operator: $KEDA_VERSION"
k get crd scaledobjects.keda.sh >/dev/null || die "KEDA CRDs missing"

# ── 3. Manifests ───────────────────────────────────────────────────────────
say "3. apply scaffolding, then the PRODUCTION manifest unedited"
k apply -f "$SCAFFOLD" >/dev/null
k wait --for=condition=Available deploy -n monitoring --all --timeout=300s >/dev/null \
  || die "monitoring stack never became Available"
k wait --for=condition=Available deploy/qe-explorer -n nexus --timeout=180s >/dev/null \
  || die "qe-explorer deployment never became Available"

# The production manifest declares a PrometheusRule; without the CRD the whole
# apply fails. Installing it is part of deploying this manifest for real.
if ! k get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1; then
  curl -sSL -o /tmp/gate4-prule-crd.yaml \
    https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.76.0/example/prometheus-operator-crd/monitoring.coreos.com_prometheusrules.yaml
  k apply --server-side -f /tmp/gate4-prule-crd.yaml >/dev/null
fi
APPLY_OUT="$(k apply -f "$PRODUCTION_MANIFEST" 2>&1)"
echo "$APPLY_OUT" | sed 's/^/   /'
echo "$APPLY_OUT" | grep -q "scaledobject.keda.sh/qe-explorer-queue-depth" \
  || die "the production ScaledObject did not apply"

# KEDA's admission webhook emits warnings about this manifest. They are captured
# as evidence rather than swallowed: they are a real finding about the deployed
# configuration, not noise.
KEDA_WARNINGS="$(echo "$APPLY_OUT" | grep -i '^Warning:' || true)"

# ── 4. Trigger connectivity ────────────────────────────────────────────────
say "4. KEDA resolves the trigger and reaches Prometheus"
READY=""
for _ in $(seq 1 40); do
  READY="$(k -n nexus get scaledobject qe-explorer-queue-depth \
            -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  [ "$READY" = "True" ] && break
  sleep 6
done
[ "$READY" = "True" ] || {
  k -n nexus describe scaledobject qe-explorer-queue-depth | tail -20
  die "ScaledObject never became Ready — KEDA could not reach the metric source"
}
echo "   ScaledObject READY=True (trigger resolved, Prometheus reachable)"

# ── helpers ────────────────────────────────────────────────────────────────
set_queue() {   # depth, oldest-wait-seconds
  local pod
  pod="$(k -n monitoring get pod -l app=queue-exporter -o jsonpath='{.items[0].metadata.name}')"
  k -n monitoring exec "$pod" -- sh -c "printf '%s' '$1' > /data/depth; printf '%s' '$2' > /data/wait"
}
replicas() { k -n nexus get deploy qe-explorer -o jsonpath='{.spec.replicas}'; }
ready_pods() { k -n nexus get pods -l app=qe-explorer \
  --field-selector=status.phase=Running -o json | python -c "import json,sys;print(len(json.load(sys.stdin)['items']))"; }

# ── 5. NEGATIVE CONTROL — idle fleet sits at the floor ─────────────────────
say "5. negative control: queue empty ⇒ fleet at minReplicaCount"
set_queue 0 0
sleep 45
BASE="$(replicas)"
echo "   replicas at queue depth 0: $BASE"
[ "$BASE" = "1" ] || die "expected the floor (1) before the queue is raised, saw $BASE — a later increase would not be attributable to KEDA"

# ── 6. THE SCALE EVENT ─────────────────────────────────────────────────────
# threshold is 2 queued crawls per replica, so a depth of 8 asks for 4.
say "6. raise queue depth 0 → 8 (threshold 2/replica ⇒ expect 4)"
set_queue 8 60
SCALED=0; OBSERVED="$BASE"; ELAPSED=0
for i in $(seq 1 40); do
  sleep 6; ELAPSED=$((ELAPSED+6))
  OBSERVED="$(replicas)"
  echo "   t=${ELAPSED}s replicas=$OBSERVED"
  if [ "$OBSERVED" -gt "$BASE" ]; then SCALED=1; fi
  [ "$OBSERVED" -ge 4 ] && break
done
[ "$SCALED" = "1" ] || {
  k -n nexus describe hpa keda-hpa-qe-explorer-queue-depth | tail -25
  die "queue depth rose but the fleet never scaled out"
}
echo "   SCALED: $BASE → $OBSERVED in ${ELAPSED}s"

# ── 7. The pods are really scheduled and running on a node ─────────────────
say "7. pods scheduled and running on nodes"
k -n nexus wait --for=condition=Ready pod -l app=qe-explorer --timeout=180s >/dev/null || true
PLACEMENT="$(k -n nexus get pods -l app=qe-explorer -o json | python -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps([{'pod':p['metadata']['name'],'node':p['spec'].get('nodeName'),
                   'phase':p['status']['phase']} for p in d['items']]))")"
echo "   $PLACEMENT"
RUNNING="$(ready_pods)"
[ "$RUNNING" -ge 2 ] || die "scaling changed spec.replicas but only $RUNNING pod(s) actually run"

HPA_STATE="$(k -n nexus get hpa keda-hpa-qe-explorer-queue-depth -o json | python -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps({'current':d['status'].get('currentReplicas'),
                  'desired':d['status'].get('desiredReplicas'),
                  'metrics':d['status'].get('currentMetrics')}))")"
SCALE_EVENTS="$(k -n nexus get events --field-selector reason=SuccessfulRescale -o json | python -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps([e['message'] for e in d['items']]))" 2>/dev/null || echo '[]')"

# ── 8. Scale-down is observed too (not asserted — it is slow by design) ────
say "8. drop the queue to 0 and record what the fleet does"
set_queue 0 0
sleep 60
AFTER_DROP="$(replicas)"
echo "   replicas 60s after the queue emptied: $AFTER_DROP (scaleDown stabilisation is 300s)"

# ── Evidence ───────────────────────────────────────────────────────────────
python - "$EVIDENCE" <<PYEOF
import json, subprocess, sys, datetime
out = {
  "milestone": "A31",
  "recorded_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
  "cluster": {"name": "$CLUSTER", "context": "$CTX", "nodes": $NODE_JSON},
  "keda": {"operator_image": "$KEDA_VERSION"},
  "manifest": {
     "path": "infrastructure/keda/qe-explorer-scaledobject.yaml",
     "applied_unedited": True,
     "admission_warnings": """$KEDA_WARNINGS""".strip().splitlines(),
  },
  "trigger": {"ready": True,
              "server": "http://prometheus-operated.monitoring:9090",
              "queries": ["sum(qec_crawl_queue_depth)",
                          "max(qec_crawl_queue_oldest_wait_seconds)"]},
  "scale_event": {
     "replicas_at_queue_0": int("$BASE"),
     "queue_depth_applied": 8,
     "replicas_observed": int("$OBSERVED"),
     "seconds_to_scale": int("$ELAPSED"),
     "running_pods": int("$RUNNING"),
     "placement": $PLACEMENT,
     "hpa_status": $HPA_STATE,
     "hpa_rescale_events": $SCALE_EVENTS,
     "replicas_60s_after_queue_emptied": int("$AFTER_DROP"),
  },
  "boundaries": [
     "The queue gauge is published by a stand-in exporter, not by qe-central's queue_drainer._publish_fleet_metrics tick.",
     "The scaled container is a sleeping busybox, not the 3GB Playwright explorer image; what is proven is the scaling control loop, not the explorer runtime.",
     "Single-node kind cluster: node placement is observed but multi-node spreading is not exercised.",
  ],
}
# encoding is EXPLICIT: this proof runs on Windows too, where Python's default
# text encoding is cp1252 and a stray non-ASCII byte turns a PASSING run into a
# UnicodeEncodeError at the last line — losing the evidence for a proof that
# had already succeeded. (Observed exactly once, on the first green A31 run.)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=2)
print("\nevidence -> " + sys.argv[1])
PYEOF

if [ "$KEEP" != "1" ]; then
  say "teardown (pass --keep to leave the cluster up)"
  MSYS_NO_PATHCONV=1 kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
fi

say "A31 PASS — queue depth drove a real scale event on a real cluster"

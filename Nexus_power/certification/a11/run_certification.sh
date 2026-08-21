#!/usr/bin/env bash
# A11 / T-WP-02 — INDEPENDENT CERTIFICATION REPRODUCER (non-author squad).
#
# Runs the issuer and the verifier in SEPARATE interpreters, because qe-explorer
# and qe-central both ship a top-level `app` package and collide in one process.
# That constraint is why the author froze a golden contract; this harness is the
# complement to it — a FRESH key and ten grant shapes the fixed golden cannot
# cover, proving interop for arbitrary grants rather than one pinned envelope.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # -> Nexus_power
HERE="$ROOT/certification/a11"
OUT="${TMPDIR:-/tmp}/a11_cert_attestations.json"

echo "== 0. the certified artifact set is unchanged =="
( cd "$ROOT/.." && sha256sum -c "$HERE/A11_SNAPSHOT.sha256" ) \
  || { echo "REFUSING: A11 sources have drifted from the certified snapshot."; exit 2; }

echo "== 1. ISSUER half (qe-central interpreter) =="
( cd "$ROOT/platform/qe-central" \
  && PYTHONPATH="$ROOT/platform/qe-central" python "$HERE/issue_side.py" ) > "$OUT"

echo "== 2. VERIFIER half (qe-explorer interpreter) =="
( cd "$ROOT/engines/qe-explorer" \
  && PYTHONPATH="$ROOT/engines/qe-explorer" python "$HERE/verify_side.py" "$OUT" )

#!/usr/bin/env bash
# A11 / T-WP-02 — INDEPENDENT CERTIFICATION REPRODUCER (non-author squad).
#
# Runs the issuer and the verifier in SEPARATE interpreters, because qe-explorer
# and qe-central both ship a top-level `app` package and collide in one process.
# That constraint is why the author froze a golden contract; this harness is the
# complement to it — a FRESH key and ten grant shapes the fixed golden cannot
# cover, proving interop for arbitrary grants rather than one pinned envelope.
#
# Exit 2 = the certified bytes moved (the record has lapsed; re-certify).
# Exit 1 = a certification check failed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # -> Nexus_power
HERE="$ROOT/certification/a11"
OUT="${TMPDIR:-/tmp}/a11_cert_attestations.json"

echo "== 0. the certified artifact set is unchanged (incl. this harness) =="
# THE MANIFEST NOW PINS THIS HARNESS TOO (CERT-FINDING-10). Until 2026-08-21 it
# pinned only the NINE files under certification and not the three that do the
# certifying. A certifier demonstrated the consequence rather than arguing it:
# change one token in issue_side.py's probe, re-assert the false KMS claim in the
# certified source, re-pin the manifest the way ANY legitimate change re-pins it
# -- and the reproducer reports all digests OK, 152 checks, 0 failures, with the
# defect fully restored. An assertion that can be neutered without leaving a trace
# is not an assertion.
#
# Not circular: this script verifies the manifest, so an edit to this script fails
# the digest check exactly as an edit to attest.py does. The residual -- someone
# edits the harness AND the manifest together -- is the same residual the
# implementation always had, and the same answer applies: a non-author re-derives
# the manifest every round.
#
# Strip CR before checking. `sha256sum -c` treats a trailing CR as part of the
# FILENAME, so on a core.autocrlf checkout every entry fails to open and is
# reported exactly the way real drift is reported. A certification tool that
# cannot tell "the bytes moved" from "your git normalised my line endings" is
# worse than no tool — so normalise, and let a failure here mean what it says.
NORM="${TMPDIR:-/tmp}/a11_snapshot.norm.sha256"
tr -d '\r' < "$HERE/A11_SNAPSHOT.sha256" > "$NORM"
if ! ( cd "$ROOT/.." && sha256sum -c "$NORM" ); then
  echo "REFUSING: A11 sources have drifted from the certified snapshot."
  echo "The certification record binds to those digests and has LAPSED."
  exit 2
fi

echo "== 1. ISSUER half (qe-central interpreter) =="
( cd "$ROOT/platform/qe-central" \
  && PYTHONPATH="$ROOT/platform/qe-central" python "$HERE/issue_side.py" ) > "$OUT"

echo "== 2. VERIFIER half (qe-explorer interpreter) =="
( cd "$ROOT/engines/qe-explorer" \
  && PYTHONPATH="$ROOT/engines/qe-explorer" python "$HERE/verify_side.py" "$OUT" )

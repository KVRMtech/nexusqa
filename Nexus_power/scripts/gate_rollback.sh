#!/usr/bin/env bash
# MULTI-SERVICE ROLLBACK (M0.4 / T-GT-01) — restore EXACTLY the deployment set.
#
# ONE implementation, used by deploy.ps1 on a red gate AND by the rollback drill.
# They used to be two: deploy.ps1 inlined a shell loop and gate_rollback_drill.sh
# re-typed it "verbatim in shape". Two copies of a rollback is one rollback that
# is never really drilled — the drill proved a COPY worked while the original
# restored one service of three.
#
# INPUT is the deployment manifest written at deploy time (gate_manifest.py), not
# a variable read at rollback time. Rollback targets are DATA, never inferred.
#
# ALL-OR-REPORT: every service in the manifest must come back. The previous code
# set ok=1 if ANY service restored and exited 0 — a rollback that reports success
# while a rejected container keeps serving is worse than no rollback at all,
# because it ends the investigation.
#
# Usage:
#   gate_rollback.sh --src <repo-root> [--manifest <path>] [--green <sha>] [--dry-run]
#
# Exit 0  every deployed service restored to the last green commit
# Exit 1  one or more services NOT restored — the fleet is mixed; intervene
# Exit 2  cannot even attempt: no manifest, no green anchor, bad arguments
set -u

SRC=""
MANIFEST=""
GREEN=""
DRY_RUN=0
NEXUS_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --src)      SRC="${2:-}"; shift 2 ;;
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --green)    GREEN="${2:-}"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    *) echo "gate_rollback.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

if [ -z "$SRC" ]; then
  echo "gate_rollback.sh: --src <repo-root> is required" >&2
  exit 2
fi
[ -n "$MANIFEST" ] || MANIFEST="$SRC/.deploy_manifest.json"
NEXUS_DIR="$SRC/Nexus_power"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Overridable so the rollback path is executable off the VM — the previous drill
# could only run against production, which is how a single-service assumption
# stayed unchallenged in it for as long as it did.
PYTHON="${PYTHON:-python3}"

say() { printf '%s\n' "$*"; }

# ── 1. The anchor: which commit are we restoring TO? ────────────────────────
if [ -z "$GREEN" ]; then
  GREEN=$(cat "$SRC/.last_green_deploy" 2>/dev/null | tr -d '\r\n ')
fi
if [ -z "$GREEN" ]; then
  say "ROLLBACK IMPOSSIBLE — no last-green commit recorded."
  say "The fleet is running an UNVERIFIED build. Roll back by hand."
  exit 2
fi

# ── 2. The inventory: which services were actually deployed? ────────────────
# A manifest we cannot parse aborts the rollback rather than guessing a subset.
PLAN=$("$PYTHON" "$HERE/gate_manifest.py" rollback-plan --manifest "$MANIFEST" 2>&1)
if [ $? -ne 0 ]; then
  say "ROLLBACK IMPOSSIBLE — $PLAN"
  say "Without a trustworthy inventory a rollback restores an arbitrary subset."
  exit 2
fi
if [ -z "$PLAN" ]; then
  say "ROLLBACK IMPOSSIBLE — the manifest records no services."
  exit 2
fi
# Strip CR before anything reads a field. A checkout made on Windows, or a python
# whose stdout is in text mode, yields CRLF; `read` then hands the loop a compose
# filename with a trailing carriage return and every `docker compose -f` fails
# with a name that LOOKS correct in the log. This repo already carries a CRLF
# hazard note for exactly this class of bug.
PLAN=$(printf '%s' "$PLAN" | tr -d '\r')

say "rollback target : $GREEN"
say "rollback order  : $(printf '%s' "$PLAN" | cut -f1 | tr '\n' ' ')"

if [ "$DRY_RUN" -eq 1 ]; then
  say "--dry-run: plan only, nothing restored."
  printf '%s\n' "$PLAN"
  exit 0
fi

# ── 3. Restore the source tree ──────────────────────────────────────────────
if ! git -C "$SRC" checkout -q "$GREEN"; then
  say "ROLLBACK FAILED — could not check out $GREEN. The fleet is on the rejected build."
  exit 1
fi

# ── 4. Rebuild + swap every service, in rollback order ──────────────────────
RESTORED=""
FAILED=""
while IFS=$'\t' read -r SVC COMPOSE; do
  [ -n "$SVC" ] || continue
  say ""
  say ">> restoring $SVC (from $COMPOSE)"
  # The manifest names the compose file, but a rolled-back tree is an OLDER tree:
  # if that commit predates the service, `config --services` will not list it and
  # rebuilding is impossible. That is a real, reportable failure — not something
  # to skip quietly.
  if ! (cd "$NEXUS_DIR" && docker compose -f "$COMPOSE" config --services 2>/dev/null \
        | grep -qx "$SVC"); then
    say "   FAIL $SVC is not defined in $COMPOSE at $GREEN"
    FAILED="$FAILED $SVC"
    continue
  fi
  if (cd "$NEXUS_DIR" \
      && docker compose -f "$COMPOSE" build "$SVC" \
      && docker compose -f "$COMPOSE" up -d --force-recreate "$SVC"); then
    say "   OK   $SVC restored"
    RESTORED="$RESTORED $SVC"
  else
    say "   FAIL $SVC did NOT restore"
    FAILED="$FAILED $SVC"
  fi
done <<EOF
$PLAN
EOF

say ""
say "--- rollback result ---"
say "restored:${RESTORED:- none}"
say "failed  :${FAILED:- none}"

if [ -n "$FAILED" ]; then
  # Naming the survivors matters more than the exit code: an operator arriving at
  # a mixed fleet needs to know WHICH containers are still on the rejected build.
  say ""
  say "ROLLBACK INCOMPLETE — these services are still on the REJECTED build:$FAILED"
  say "The fleet is MIXED. Intervene now; do not ship anything else."
  exit 1
fi
say "ROLLBACK COMPLETE — every deployed service is back on $GREEN."
exit 0

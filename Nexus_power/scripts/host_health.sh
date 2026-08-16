#!/usr/bin/env bash
# HOST HEALTH — is this machine in a state where a deployment or a crawl can be
# BELIEVED?
#
# Extracted from golden_crawl_gate.sh (M0.4 / T-GT-05) for two reasons.
#
# 1. IT NOW RUNS BEFORE THE SWAP. The rollback decision matrix says an
#    infrastructure failure must never revert a healthy deployment — but the gate
#    runs AFTER the container swap, so by the time a full disk was noticed the
#    fleet had already changed. Running the same check as a PREFLIGHT means the
#    most common infra failure aborts while the previous build is still serving,
#    and nothing has to be reverted at all. Post-swap it still runs, and there it
#    aborts without rolling back.
#
# 2. ONE DEFINITION OF HEALTHY. A preflight that checked something subtly
#    different from the gate would let a deploy through a door the gate then
#    refuses — the worst of both.
#
# A crawl on an unhealthy host produces a CONFUSING result, not an obviously
# broken one: a full disk truncates evidence, a dead container refuses dispatch
# in a way that reads like an app problem, and hundreds of exited containers
# quietly eat the loop device table. Failing fast costs 2 seconds and saves the
# investigation that a half-degraded crawl always triggers.
#
# Usage:  host_health.sh [container ...]      # defaults to the QEC fleet
# Exit 0 = healthy. Exit 4 = HOST UNAVAILABLE (never a verdict on the build).
set -u

HEALTH_DISK_MAX_PCT="${HEALTH_DISK_MAX_PCT:-90}"
HEALTH_ZOMBIE_MAX="${HEALTH_ZOMBIE_MAX:-100}"
EXIT_HOST_UNAVAILABLE=4

say() { printf '%s\n' "$*"; }

host_health() {  # container names as arguments
  local bad=0 disk zombies c
  disk=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
  if [ "${disk:-0}" -ge "$HEALTH_DISK_MAX_PCT" ]; then
    say "FAIL  host: disk ${disk}% full — evidence capture will truncate silently"
    bad=1
  else
    say "OK    host: disk ${disk:-?}% used"
  fi
  zombies=$(docker ps -q -f status=exited 2>/dev/null | wc -l | tr -d ' ')
  if [ "${zombies:-0}" -ge "$HEALTH_ZOMBIE_MAX" ]; then
    say "FAIL  host: $zombies exited containers — prune before trusting a crawl"
    bad=1
  else
    say "OK    host: $zombies exited containers"
  fi
  for c in "$@"; do
    [ -n "$c" ] || continue
    if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" != "true" ]; then
      say "FAIL  host: container $c is not running"
      bad=1
    fi
  done
  return $bad
}

# Only act as a CLI when EXECUTED. Sourced (by golden_crawl_gate.sh) it just
# defines host_health, so the gate keeps its own framing and exit codes.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  set -- "${@:-}"
  if [ $# -eq 0 ] || [ -z "${1:-}" ]; then
    set -- nexus-qe-central nexus-postgres nexus-qe-explorer
  fi
  say "=== HOST HEALTH PREFLIGHT ==="
  if host_health "$@"; then
    say "HOST HEALTHY — safe to deploy."
    exit 0
  fi
  say ""
  say "HOST UNAVAILABLE — aborting BEFORE the swap, so nothing needs reverting."
  say "This is not a verdict on any build."
  exit $EXIT_HOST_UNAVAILABLE
fi

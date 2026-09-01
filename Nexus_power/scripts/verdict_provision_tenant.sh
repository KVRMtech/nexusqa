#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# VKPower Verdict — onboard a CLIENT TENANT from the box shell (Phase-7 fleet).
#
# The deploy runbook's "add client #2..#20" step: docker-execs into the running
# qe-central container and calls app.fleet.provisioning.provision_tenant() DIRECTLY
# (in-process, against the real qecentral + nexus DBs).  Box-shell access IS the
# operator authority here — the same posture as verdict_box_bootstrap.sh — so no
# platform-admin JWT is needed for the CLI path (the HTTP /api/v1/qec/tenants route
# is the token-gated equivalent for remote operators).
#
# The Phase-6 safety spine still applies IN-PROCESS: in a deployed env wearing dev
# defaults (dev KEK / default secrets) provisioning FAIL-CLOSES and this script
# exits non-zero — it never onboards a client onto an unsafe stack.
#
# IDEMPOTENT: pass --tenant-id to re-run for the same tenant (returns the handle,
# created=false).  Prints the onboarding handle as JSON INCLUDING the tenant's
# first-admin bootstrap token — capture it once; it is never stored in plaintext
# and never logged.
#
# Usage:
#   scripts/verdict_provision_tenant.sh \
#     --name "Acme Insurance" --admin-email admin@acme.example [--plan starter] \
#     [--tenant-id <uuid>] [--domain acme.verdict.internal] [--container nexus-qe-central]
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

CONTAINER="${VERDICT_QEC_CONTAINER:-nexus-qe-central}"
NAME=""
ADMIN_EMAIL=""
PLAN="starter"
TENANT_ID=""
DOMAIN=""
ACTOR="${USER:-operator}@box-cli"

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --name)         NAME="${2:-}"; shift 2 ;;
    --admin-email)  ADMIN_EMAIL="${2:-}"; shift 2 ;;
    --plan)         PLAN="${2:-}"; shift 2 ;;
    --tenant-id)    TENANT_ID="${2:-}"; shift 2 ;;
    --domain)       DOMAIN="${2:-}"; shift 2 ;;
    --container)    CONTAINER="${2:-}"; shift 2 ;;
    --actor)        ACTOR="${2:-}"; shift 2 ;;
    -h|--help)      usage 0 ;;
    *) echo "FATAL: unknown argument: $1" >&2; usage 1 ;;
  esac
done

[ -n "$NAME" ]        || { echo "FATAL: --name is required" >&2; exit 1; }
[ -n "$ADMIN_EMAIL" ] || { echo "FATAL: --admin-email is required" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || { echo "FATAL: docker not found on PATH" >&2; exit 1; }
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" \
  || { echo "FATAL: container '$CONTAINER' is not running (set --container / VERDICT_QEC_CONTAINER)" >&2; exit 1; }

echo "==== VERDICT PROVISION TENANT @ $(date -u +%FT%TZ) · container=$CONTAINER ===="
echo "name='$NAME' plan='$PLAN' admin_email='$ADMIN_EMAIL' tenant_id='${TENANT_ID:-<new>}'"

# The provisioning runs IN-PROCESS inside the container (real DBs + the safety
# spine).  Args are passed as env vars (never interpolated into the python source)
# so a name/email with shell-special characters can never break out.
docker exec \
  -e P7_NAME="$NAME" \
  -e P7_EMAIL="$ADMIN_EMAIL" \
  -e P7_PLAN="$PLAN" \
  -e P7_TENANT_ID="$TENANT_ID" \
  -e P7_DOMAIN="$DOMAIN" \
  -e P7_ACTOR="$ACTOR" \
  "$CONTAINER" python -c '
import asyncio, json, os, sys

from app.fleet.provisioning import provision_tenant, ProvisioningError


async def _main() -> int:
    try:
        handle = await provision_tenant(
            os.environ["P7_NAME"],
            os.environ.get("P7_PLAN", "starter"),
            os.environ["P7_EMAIL"],
            tenant_id=(os.environ.get("P7_TENANT_ID") or None),
            domain=(os.environ.get("P7_DOMAIN") or None),
            actor=(os.environ.get("P7_ACTOR") or "operator"),
        )
    except ProvisioningError as exc:
        print(f"PROVISION_REFUSED ({exc.status_code}): {exc.message}", file=sys.stderr)
        return 2
    except Exception as exc:  # honest, non-zero — never a silent partial onboard
        print(f"PROVISION_ERROR: {exc!s}", file=sys.stderr)
        return 3
    print(json.dumps(handle.as_dict(include_token=True), indent=2, sort_keys=True))
    return 0


sys.exit(asyncio.run(_main()))
'
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "==== PROVISION FAILED (exit $rc) — see the error above ====" >&2
  exit "$rc"
fi
echo "==== PROVISION OK — capture the admin_token above (it is shown ONCE) ===="

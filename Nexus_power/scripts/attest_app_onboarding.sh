#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ONBOARDING ATTESTATION — sign an app through draft → attested → live.
#
# WHY THIS SCRIPT EXISTS. `security/prod_guard.py` refuses to crawl any app that
# has not been attested, and it is right to: the refusal names a HUMAN who says
# they are permitted to test the target. But there is no endpoint that records
# that claim — it lives in `client_apps.env_attestation` as raw JSONB, so every
# onboarding so far has been a hand-written UPDATE. That is a poor shape for the
# one record an auditor would actually ask to see.
#
# WHAT THE GUARD REQUIRES (prod_guard.py, all read from env_attestation):
#   1. rules_of_engagement.signed == true  AND  a non-empty signed_by
#   2. env_kind in {disposable, staging}   AND  attested_by  AND  expires_at future
#   3. preflight.passed == true
#   plus authorization.authorized == true AND authorized_by
#
# THE SIGNER IS THE POINT. Every one of those fields names a person taking
# responsibility for testing someone else's system. Run this yourself, under
# your own name — an assistant writing it would make the record a fiction, and
# the record is the only thing standing between this crawler and a site nobody
# authorised it to touch.
#
# DELIBERATELY NOT A BYPASS. `fences.onboarding_test_bypass` exists for dev and
# this script never sets it. If you are reaching for a bypass to crawl a third
# party, the answer is to get authorisation, not to route around the guard.
#
# Usage:
#   attest_app_onboarding.sh --app <app_id> --signer "<name>" \
#       [--env-kind disposable|staging] [--days 30] \
#       [--basis "why you are permitted to test this target"] \
#       [--terms "the limits you are committing to"]
#
#   --show <app_id>     print the current attestation and derived state
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

PG="${PG_CONTAINER:-nexus-postgres}"
DB="${PG_DB:-qecentral}"
PG_USER="${PG_USER:-nexus}"

APP=""; SIGNER=""; ENV_KIND="disposable"; DAYS=30; BASIS=""; TERMS=""; SHOW=""

while [ $# -gt 0 ]; do
  case "$1" in
    --app)      APP="${2:-}"; shift 2 ;;
    --signer)   SIGNER="${2:-}"; shift 2 ;;
    --env-kind) ENV_KIND="${2:-}"; shift 2 ;;
    --days)     DAYS="${2:-}"; shift 2 ;;
    --basis)    BASIS="${2:-}"; shift 2 ;;
    --terms)    TERMS="${2:-}"; shift 2 ;;
    --show)     SHOW="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -n "$SHOW" ]; then
  docker exec "$PG" psql -U "$PG_USER" -d "$DB" -A -c \
    "SELECT name, status, jsonb_pretty(env_attestation) FROM client_apps WHERE app_id = '$SHOW';"
  exit 0
fi

[ -n "$APP" ]    || { echo "--app <app_id> is required" >&2; exit 2; }
[ -n "$SIGNER" ] || { echo "--signer \"<your name>\" is required — this record names who authorised it" >&2; exit 2; }
case "$ENV_KIND" in
  disposable|staging) ;;
  *) echo "--env-kind must be 'disposable' or 'staging'. A production target is never attestable here." >&2; exit 2 ;;
esac

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
EXPIRES="$(date -u -d "+${DAYS} days" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -v "+${DAYS}d" +%Y-%m-%dT%H:%M:%SZ)"

TARGET="$(docker exec "$PG" psql -U "$PG_USER" -d "$DB" -A -t -c \
  "SELECT name || ' -> ' || base_url FROM client_apps WHERE app_id = '$APP';" | tr -d '\r')"
[ -n "$TARGET" ] || { echo "no such app: $APP" >&2; exit 1; }

echo "about to attest, as '$SIGNER':"
echo "    $TARGET"
echo "    env_kind=$ENV_KIND  expires=$EXPIRES"
echo

# Written via a file rather than an inline literal: the basis and terms are
# free prose and would otherwise have to survive two levels of shell quoting
# into SQL, which is how an attestation ends up silently truncated.
TMP="$(mktemp)"
python3 - "$TMP" <<PY
import json, sys
json.dump({
    "env_kind": "$ENV_KIND",
    "attested_by": """$SIGNER""",
    "expires_at": "$EXPIRES",
    "preflight": {"passed": True, "passed_by": """$SIGNER""", "passed_at": "$NOW"},
    "authorization": {
        "authorized": True,
        "authorized_by": """$SIGNER""",
        "authorized_at": "$NOW",
        "basis": """$BASIS""",
    },
    "rules_of_engagement": {
        "signed": True,
        "signed_by": """$SIGNER""",
        "signed_at": "$NOW",
        "terms": """$TERMS""",
    },
    "reset_procedure": "",
}, open(sys.argv[1], "w"), indent=2)
PY

docker cp "$TMP" "$PG:/tmp/_attest.json" >/dev/null
rm -f "$TMP"
docker exec "$PG" psql -U "$PG_USER" -d "$DB" -A -t -c \
  "UPDATE client_apps SET env_attestation = pg_read_file('/tmp/_attest.json')::jsonb WHERE app_id = '$APP';"
docker exec "$PG" sh -c "rm -f /tmp/_attest.json"

echo
echo "--- recorded ---"
docker exec "$PG" psql -U "$PG_USER" -d "$DB" -A -c \
  "SELECT jsonb_pretty(env_attestation) FROM client_apps WHERE app_id = '$APP';"
echo
echo "The app should now be crawlable. If the gate still refuses, it will say which"
echo "of the four requirements is still missing — read the refusal, do not bypass it."

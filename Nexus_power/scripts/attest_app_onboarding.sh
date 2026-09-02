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

# Built as a SQL FILE read by the psql CLIENT, not via pg_read_file(). The
# server runs as `postgres` and `docker cp` lands files owned by root, so
# pg_read_file() fails with "Permission denied" while reporting success around
# it — the first run of this script wrote an EMPTY attestation that way and the
# guard would have gone on refusing with no clue why.
#
# Values are passed as argv, never interpolated into the heredoc: a basis or
# terms string containing an apostrophe would otherwise break out of the shell
# quoting, the SQL quoting, or both.
TMP="$(mktemp)"
python3 - "$TMP" "$APP" "$SIGNER" "$ENV_KIND" "$EXPIRES" "$NOW" "$BASIS" "$TERMS" <<'PY'
import json, sys
tmp, app, signer, env_kind, expires, now, basis, terms = sys.argv[1:9]
doc = {
    "env_kind": env_kind,
    "attested_by": signer,
    "expires_at": expires,
    "preflight": {"passed": True, "passed_by": signer, "passed_at": now},
    "authorization": {
        "authorized": True, "authorized_by": signer,
        "authorized_at": now, "basis": basis,
    },
    "rules_of_engagement": {
        "signed": True, "signed_by": signer, "signed_at": now, "terms": terms,
    },
    "reset_procedure": "",
}
lit = json.dumps(doc).replace("'", "''")          # SQL single-quote escaping
with open(tmp, "w", encoding="utf-8") as fh:
    sql = "UPDATE client_apps SET env_attestation = '%s'::jsonb WHERE app_id = '%s';" % (
        lit, app.replace("'", "''"))
    fh.write(sql + chr(10))
PY

docker cp "$TMP" "$PG:/tmp/_attest.sql" >/dev/null
rm -f "$TMP"
OUT=$(docker exec "$PG" psql -U "$PG_USER" -d "$DB" -A -t -v ON_ERROR_STOP=1 -f /tmp/_attest.sql 2>&1)
RC=$?
docker exec "$PG" sh -c "rm -f /tmp/_attest.sql"
if [ $RC -ne 0 ] || [ "${OUT#UPDATE }" = "$OUT" ]; then
  echo "ATTESTATION NOT WRITTEN: $OUT" >&2
  exit 1
fi
echo "  $OUT"

# A write that reports success and stores nothing is the failure mode that made
# the first version of this script useless. Prove the record is really there.
CHECK=$(docker exec "$PG" psql -U "$PG_USER" -d "$DB" -A -t -c   "SELECT (env_attestation->'authorization'->>'authorized') || '/' || coalesce(env_attestation->>'attested_by','') FROM client_apps WHERE app_id = '$APP';" | tr -d ' ')
if [ "$CHECK" != "true/$SIGNER" ]; then
  echo "ATTESTATION DID NOT PERSIST (read back: '$CHECK')" >&2
  exit 1
fi

echo
echo "--- recorded ---"
docker exec "$PG" psql -U "$PG_USER" -d "$DB" -A -c \
  "SELECT jsonb_pretty(env_attestation) FROM client_apps WHERE app_id = '$APP';"
echo
echo "The app should now be crawlable. If the gate still refuses, it will say which"
echo "of the four requirements is still missing — read the refusal, do not bypass it."

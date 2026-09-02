#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# TENANT ISOLATION PROOF — two real tenants, real rows, and a control that leaks.
#
# WHY THIS EXISTS. Every app and every crawl in production ran under ONE tenant
# (__platform__: 14 apps, 605 crawls, measured 2026-09-02). RLS was enabled and
# FORCED on 40 tables and the unit tests were green — but nothing had ever had
# to be kept APART, so the guarantee was untested where it counts. The first
# paying client #2 would have been the first real test.
#
# WHAT IT CHECKS, and why each half is needed:
#   * the API layer — a tenant's own endpoint must not return another's rows;
#   * RLS ALONE — the same read with the application's `WHERE tenant_id = ...`
#     REMOVED. That filter is defence in depth, and testing only the API proves
#     the filter works, not the database guarantee underneath it. RLS is what
#     still holds the day someone forgets the WHERE clause.
#
# THE CONTROL IS THE POINT. Every assertion here is an ABSENCE ("tenant A sees
# nothing"), and an absence is satisfied just as well by a row that was never
# written or a query that never ran. So the last step re-reads the SAME row as a
# superuser (rolbypassrls=t) and REQUIRES it to appear. If the control stops
# leaking, this script is no longer evidence of anything and says so.
#
# Usage:  bash tenant_isolation_proof.sh          # cleans up after itself
#         KEEP=1 bash tenant_isolation_proof.sh   # leave the proof app behind
# ─────────────────────────────────────────────────────────────────────────────
set -u
QEC=nexus-qe-central; PG=nexus-postgres
TA=__platform__
TB=proof-tenant-b

api() {  # $1 tenant  $2 method  $3 path  $4 body(or empty)
  docker exec $QEC python -c "
import os, json, time, urllib.request, jwt
sec = os.environ['NEXUS_JWT_SECRET']
tok = jwt.encode({'sub':'iso-proof','tenant_id':'$1','email':'p@nexus.internal',
                  'role':'admin','exp':int(time.time())+3600}, sec, algorithm='HS256')
if isinstance(tok, bytes): tok = tok.decode()
body = '''$4'''
data = body.encode() if body.strip() else None
req = urllib.request.Request('http://localhost:8093$3', data=data, method='$2',
    headers={'Authorization':'Bearer '+tok,'Content-Type':'application/json'})
try:
    print(urllib.request.urlopen(req, timeout=60).read().decode())
except urllib.error.HTTPError as e:
    print('HTTP_%d %s' % (e.code, e.read().decode()[:400]))
"
}

# raw DB read as the APPLICATION's role, with the tenant GUC set by hand and NO
# WHERE clause on tenant — this is RLS alone, with the app's filter removed.
as_qec() {  # $1 tenant  $2 app_id
  docker exec $PG psql -U qec -d qecentral -A -t -c \
    "SELECT set_config('nexus.current_tenant_id','$1',false); SELECT count(*) FROM client_apps WHERE app_id='$2';" \
    2>&1 | tail -1 | tr -d ' \r'
}

echo "=== 1. create an app under a SECOND tenant ($TB) ==="
CREATED=$(api "$TB" POST /api/v1/qec/apps '{"name":"Isolation Proof App","base_url":"https://example.invalid/proof"}')
echo "$CREATED" | cut -c1-200
APPID=$(echo "$CREATED" | python3 -c "import sys,json;print(json.load(sys.stdin).get('app_id',''))" 2>/dev/null)
[ -n "$APPID" ] || { echo "could not create app under $TB"; exit 1; }
echo "app_id: $APPID"

echo
echo "=== 2. API LAYER — can tenant A see it? ==="
A_SEES=$(api "$TA" GET /api/v1/qec/apps "" | grep -c "$APPID" || true)
B_SEES=$(api "$TB" GET /api/v1/qec/apps "" | grep -c "$APPID" || true)
echo "  tenant A ($TA) sees it: $A_SEES   (must be 0)"
echo "  tenant B ($TB) sees it: $B_SEES   (must be 1)"

echo
echo "=== 3. DATABASE LAYER — RLS alone, app's WHERE clause removed ==="
R_A=$(as_qec "$TA" "$APPID")
R_B=$(as_qec "$TB" "$APPID")
echo "  as role qec, GUC=$TA : $R_A   (must be 0)"
echo "  as role qec, GUC=$TB : $R_B   (must be 1)"

echo
echo "=== 4. CONTROL — remove the guard; the row MUST become visible ==="
echo "     (superuser 'nexus' has rolbypassrls=t, so RLS does not apply)"
CTRL=$(docker exec $PG psql -U nexus -d qecentral -A -t -c \
  "SELECT count(*) FROM client_apps WHERE app_id='$APPID';" 2>&1 | tr -d ' \r')
echo "  as superuser (RLS bypassed): $CTRL   (must be 1 - proves the row EXISTS)"

echo

# ── WRITES (all inside BEGIN ... ROLLBACK) ──────────────────────────────────
VICTIM=$(docker exec $PG psql -U nexus -d qecentral -A -t -c "SELECT app_id FROM client_apps WHERE tenant_id='$TA' ORDER BY created_at LIMIT 1;" | tr -d ' ')
q() { docker exec $PG psql -U "$1" -d qecentral -A -t -c "$2" 2>&1 | tr -d ''; }

echo
echo "=== 5. WRITES — can $TB touch $TA's rows? ==="
echo "  target row owned by $TA: $VICTIM"

W1=$(q qec "BEGIN; SELECT set_config('nexus.current_tenant_id','$TB',true); INSERT INTO client_apps (app_id, tenant_id, name, base_url) VALUES ('11111111-1111-1111-1111-111111111111','$TA','forged','https://x.invalid'); ROLLBACK;")
echo "$W1" | grep -qiE "row-level security|violates" && W1R=REFUSED || W1R=ACCEPTED
echo "  forge a row for another tenant : $W1R    (must be REFUSED)"

W2=$(q qec "BEGIN; SELECT set_config('nexus.current_tenant_id','$TB',true); UPDATE client_apps SET name='hijacked' WHERE app_id='$VICTIM'; ROLLBACK;")
W2R=$(echo "$W2" | grep -oE 'UPDATE [0-9]+' | tail -1)
echo "  update another tenant's row    : $W2R    (must be UPDATE 0)"

W3=$(q qec "BEGIN; SELECT set_config('nexus.current_tenant_id','$TB',true); DELETE FROM client_apps WHERE app_id='$VICTIM'; ROLLBACK;")
W3R=$(echo "$W3" | grep -oE 'DELETE [0-9]+' | tail -1)
echo "  delete another tenant's row    : $W3R    (must be DELETE 0)"

W4=$(q qec "BEGIN; SELECT set_config('nexus.current_tenant_id','$TA',true); UPDATE client_apps SET tenant_id='$TB' WHERE app_id='$VICTIM'; ROLLBACK;")
echo "$W4" | grep -qiE "row-level security|violates" && W4R=REFUSED || W4R=ACCEPTED
echo "  hand your own row to another   : $W4R    (must be REFUSED)"

# control_mechanics is the ONE policy with no explicit WITH CHECK. Postgres
# reuses USING as the check for an ALL policy - asserted here rather than
# trusted, because the docs are not a statement about THIS database.
W5=$(q qec "BEGIN; SELECT set_config('nexus.current_tenant_id','$TA',true); UPDATE control_mechanics SET tenant_id='$TB'; ROLLBACK;")
echo "$W5" | grep -qiE "row-level security|violates" && W5R=REFUSED || W5R=ACCEPTED
echo "  control_mechanics re-tenant    : $W5R    (must be REFUSED)"

# CONTROL for the write half: without it, every "0 rows" above is also what you
# would see if the target row simply did not exist.
W6=$(q nexus "BEGIN; UPDATE client_apps SET name='control-probe' WHERE app_id='$VICTIM'; ROLLBACK;")
W6R=$(echo "$W6" | grep -oE 'UPDATE [0-9]+' | tail -1)
echo "  CONTROL, RLS bypassed          : $W6R    (must be UPDATE 1)"

PERSISTED=$(q nexus "SELECT count(*) FROM client_apps WHERE name IN ('hijacked','forged','control-probe');")
echo "  rows persisted by this section : $PERSISTED    (must be 0)"

echo
echo "=== VERDICT ==="
FAIL=0
[ "$A_SEES" = "0" ] || { echo "  FAIL: tenant A saw tenant B's app through the API"; FAIL=1; }
[ "$B_SEES" = "1" ] || { echo "  FAIL: tenant B could not see its own app"; FAIL=1; }
[ "$R_A" = "0" ]    || { echo "  FAIL: RLS did not hide the row from tenant A"; FAIL=1; }
[ "$R_B" = "1" ]    || { echo "  FAIL: RLS hid the row from its OWNING tenant"; FAIL=1; }
[ "$CTRL" = "1" ]   || { echo "  FAIL: the control did not see the row - the row may not exist,"; \
                         echo "        which would make every 0 above meaningless"; FAIL=1; }
[ "$W1R" = "REFUSED" ]  || { echo "  FAIL: a tenant forged a row for another tenant"; FAIL=1; }
[ "$W2R" = "UPDATE 0" ] || { echo "  FAIL: a tenant updated another tenant's row"; FAIL=1; }
[ "$W3R" = "DELETE 0" ] || { echo "  FAIL: a tenant deleted another tenant's row"; FAIL=1; }
[ "$W4R" = "REFUSED" ]  || { echo "  FAIL: a tenant handed its row to another tenant"; FAIL=1; }
[ "$W5R" = "REFUSED" ]  || { echo "  FAIL: control_mechanics accepted a cross-tenant write"; FAIL=1; }
[ "$W6R" = "UPDATE 1" ] || { echo "  FAIL: the write CONTROL did not succeed - every 0 above is meaningless"; FAIL=1; }
[ "$PERSISTED" = "0" ]  || { echo "  FAIL: this proof PERSISTED writes to production"; FAIL=1; }
[ "$FAIL" = "0" ] && echo "  TENANT ISOLATION HOLDS — reads AND writes, API layer AND RLS, controls leaking."

# Clean up by default: a multi-tenant system should not accumulate fake tenants,
# and the EVIDENCE is this transcript, not the row. KEEP=1 retains it.
if [ "${KEEP:-0}" = "1" ]; then
  echo
  echo "  KEEP=1 — proof app $APPID retained under $TB"
else
  docker exec $PG psql -U nexus -d qecentral -A -t -c     "DELETE FROM client_apps WHERE app_id='$APPID';" >/dev/null 2>&1
  LEFT=$(docker exec $PG psql -U nexus -d qecentral -A -t -c     "SELECT count(*) FROM client_apps WHERE app_id='$APPID';" 2>&1 | tr -d ' ')
  echo
  echo "  cleaned up: proof app rows remaining = $LEFT (expected 0)"
fi
exit $FAIL

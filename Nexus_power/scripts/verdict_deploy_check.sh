#!/usr/bin/env bash
# Deploy-drift check: for every hot-deployed source file, assert the SERVING
# process started AFTER the file was written. Python re-imports on restart and
# node caches require()d modules, so a docker cp without a restart leaves the old
# code serving while the new file sits on disk — twice this session that cost a
# wasted round-trip and a wrong diagnosis.
set -u
fail=0
check() {  # container  file  restart_hint
  local c="$1" f="$2"
  local fm cs
  fm=$(docker exec "$c" stat -c %Y "$f" 2>/dev/null) || { echo "  MISSING $c:$f"; fail=1; return; }
  cs=$(date -u -d "$(docker inspect "$c" --format '{{.State.StartedAt}}' | cut -c1-19)" +%s)
  if [ "$cs" -gt "$fm" ]; then
    printf "  OK    %-22s %s\n" "$c" "$(basename "$f")"
  else
    printf "  STALE %-22s %s  <-- docker restart %s\n" "$c" "$(basename "$f")" "$c"
    fail=1
  fi
}
echo "=== platform-api (python re-imports on restart) ==="
for f in app/routers/test_factory.py \
         app/services/test_factory/draft_recording.py \
         app/services/test_factory/member_data_resolver.py \
         app/services/test_factory/value_harvest.py \
         app/services/test_factory/login_observation.py \
         app/services/test_factory/login_recorder.py \
         app/services/test_factory/login_fingerprint.py \
         app/services/test_factory/persona_store.py \
         app/services/test_factory/environment_routing.py \
         app/services/test_factory/card_contract.py \
         app/services/script_factory/compiler.py \
         app/services/script_factory/runner_client.py ; do
  check nexus-platform-api "/app/service/$f"
done
echo "=== runner (node CACHES require()d modules) ==="
for f in server.js login_observer.js ground_truth_recorder.js ; do
  check nexus-runner "/opt/runner/$f"
done
[ "$fail" -eq 0 ] && echo "ALL SERVING CODE IS CURRENT" || echo "DRIFT DETECTED — restart the containers named above"
exit $fail

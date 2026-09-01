#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# run_root_tests.sh — run the root `tests/` suite with PER-DIRECTORY process
# isolation, the same doctrine ci/run_platform_api_tests.sh applies per file.
#
# WHY. Several services in this repo each ship a top-level package literally
# named `app` (platform/api/app, products/nexus-qa-orchestrator/app,
# engines/*/app). Whichever one is imported first wins `sys.modules["app"]` for
# the WHOLE interpreter, so a single `pytest tests/` run makes later suites
# import a different service than they meant:
#
#   tests/engines/test_nerves_modules.py imports app.connectors.jira
#     -> binds `app` to nerves-engine
#   tests/orchestrator/* then imports app.workflows
#     -> ModuleNotFoundError: No module named 'app.workflows'
#
# The failures move with collection ORDER, which is why they read as flaky. One
# process per directory removes the shared namespace entirely: each shard binds
# `app` once, to the service it actually tests.
#
# Coverage is combined across shards (parallel `--cov-append` into one .coverage
# would race), so the reported number still covers the whole suite.
#
# NO SILENT TRUNCATION: the shard list is DISCOVERED, not hardcoded. A new
# tests/<dir>/ is picked up automatically, and the run fails if discovery finds
# nothing. Env-driven, no hardcoded paths.  Usage: bash ci/run_root_tests.sh
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

# e2e needs a live stack (integration.yml covers it); everything else runs here.
EXCLUDE_DIRS="${ROOT_TESTS_EXCLUDE:-e2e}"

mapfile -t SHARDS < <(
  find tests -mindepth 1 -maxdepth 1 -type d -not -name '__pycache__' -printf '%f\n' \
    | sort
)
[ "${#SHARDS[@]}" -gt 0 ] || { echo "FATAL: no test directories discovered under tests/." >&2; exit 1; }

# Loose test modules directly under tests/ (e.g. test_audit_fixes.py) are a shard
# of their own, so nothing in the tree goes unrun.
mapfile -t LOOSE < <(find tests -mindepth 1 -maxdepth 1 -name 'test_*.py' | sort)

echo "== root tests: ${#SHARDS[@]} director(ies) + ${#LOOSE[@]} loose module(s), one process each =="

fail=0
failed=()
skipped=()
COV_DIR="${RUNNER_TEMP:-/tmp}/root-cov"
rm -rf "$COV_DIR" && mkdir -p "$COV_DIR"

run_shard() {
  local label="$1"; shift
  local out rc summary
  out="$(COVERAGE_FILE="$COV_DIR/.coverage.$label" python -m pytest "$@" \
          -p no:cacheprovider --no-header -q --tb=short \
          --cov=sdk --cov=engines --cov=platform --cov=products \
          --cov-report= 2>&1)"
  rc=$?
  summary="$(printf '%s\n' "$out" | grep -E '[0-9]+ (passed|failed|error|skipped|deselected)' | tail -1)"
  # rc 5 == "no tests collected", which is a legitimately empty shard, not a break.
  if [ $rc -eq 5 ]; then
    printf '  %-22s SKIP (no tests collected)\n' "$label"
    skipped+=("$label")
    return 0
  fi
  if [ $rc -ne 0 ]; then
    printf '  %-22s FAIL  %s\n' "$label" "${summary:-exit $rc}"
    printf '%s\n' "$out" | tail -40
    failed+=("$label")
    fail=1
  else
    printf '  %-22s ok    %s\n' "$label" "${summary:-passed}"
  fi
}

# Directories whose collision is INTRA-directory: two files in the same shard
# import a different service's `app`, so one process per DIRECTORY is not enough
# and they need one process per FILE (what ci/run_platform_api_tests.sh does).
# tests/platform_services mixes gateway, platform-api and qi-portal modules.
# tests/engines has the SAME intra-directory collision, one per engine: ears-engine
# and eyes-engine each ship their own top-level `app`, so whichever file imports
# first owns sys.modules["app"] for the rest of the shard. Measured: as ONE process
# tests/engines is "38 failed, 671 passed"; as one process per FILE it is 22 files
# all green. The individual tests were never broken — test_eyes_modules.py
# ::TestProbeVideo passes on its own and fails in the shard, which is the
# signature of import pollution rather than a defect.
PER_FILE_DIRS="${ROOT_TESTS_PER_FILE_DIRS:-platform_services engines}"

for d in "${SHARDS[@]}"; do
  case " $EXCLUDE_DIRS " in
    *" $d "*) printf '  %-22s excluded (%s)\n' "$d" "needs a live stack"; continue ;;
  esac
  case " $PER_FILE_DIRS " in
    *" $d "*)
      printf '  %-22s (per-file isolation)\n' "$d"
      while IFS= read -r f; do
        run_shard "$d/$(basename "$f" .py)" "$f"
      done < <(find "tests/$d" -name 'test_*.py' | sort)
      continue ;;
  esac
  run_shard "$d" "tests/$d"
done

if [ "${#LOOSE[@]}" -gt 0 ]; then
  run_shard "_loose_modules" "${LOOSE[@]}"
fi

# Combine the per-shard coverage data into the single report CI publishes.
if command -v coverage >/dev/null 2>&1; then
  ( cd "$COV_DIR" && coverage combine >/dev/null 2>&1 || true )
  COVERAGE_FILE="$COV_DIR/.coverage" coverage xml -o "$REPO_ROOT/coverage.xml" >/dev/null 2>&1 || true
  COVERAGE_FILE="$COV_DIR/.coverage" coverage report --skip-empty 2>/dev/null | tail -25 || true
fi

echo
if [ $fail -ne 0 ]; then
  echo "ROOT TESTS FAILED in: ${failed[*]}" >&2
  exit 1
fi
echo "ROOT TESTS PASSED (${#SHARDS[@]} shard(s); empty: ${skipped[*]:-none})"

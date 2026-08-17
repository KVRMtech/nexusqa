#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# run_platform_api_tests.sh — run the platform/api test suite in CI with
# PER-FILE process isolation.
#
# These 300+ tests (the value/outcome oracles, the auditor, the extractors, the
# governance contracts) had never run in CI because the repo pytest.ini scopes
# `testpaths = tests` to the top-level suite only. Wiring them in surfaced real
# regressions (see docs/FINDINGS_PLATFORM_API_REGRESSIONS_2026-07-21.md), now
# pinned as strict-xfail in platform/api/tests/conftest.py.
#
# WHY per-file isolation: a few tests mutate sys.modules in ways that corrupt
# LATER files sharing the same interpreter (a cross-file import-pollution that
# makes a single `pytest tests/` run flap). Giving each file its own pytest
# process makes the suite deterministic. A file fails the gate if it has a real
# failure/error OR a strict-xfail XPASS (a regression that got fixed and whose
# stale xfail marker must now be removed).
#
# Env-driven, no hardcoded paths. Usage:  bash ci/run_platform_api_tests.sh
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # -> Nexus_power/
SERVICE_DIR="${REPO_ROOT}/platform/api"

# RUN FROM THE SERVICE ROOT, not from Nexus_power/.
#
# Several of these tests are SOURCE-READING contracts — they open the production
# module and assert on its text (that two wildcard sets are identical in both
# files, that nothing in a module reaches the network, that a router defines the
# identity label). They spell those paths relative to the CURRENT DIRECTORY:
#
#     open("app/routers/test_factory.py")
#     open("app/services/test_factory/login_observation.py")
#
# Run from Nexus_power/ that is a FileNotFoundError, so the file died at
# collection and the gate reported it as a failing test. Run from platform/api/
# the same files pass untouched: test_run_identity 11 passed (was a collection
# error), test_wildcard_host_domain 10 passed (was 2 failed), test_persona_identity
# 18 passed. Nothing about the tests changed — only where pytest was standing.
#
# PYTHONPATH still names the service dir explicitly rather than relying on the
# CWD, so `import app...` resolves the same way it did before.
cd "$SERVICE_DIR" || exit 2
export PYTHONPATH="${SERVICE_DIR}${PYTHONPATH:+:$PYTHONPATH}"
export NEXUS_SECRET_KEY="${NEXUS_SECRET_KEY:-ci-not-for-prod}"
export NEXUS_JWT_SECRET="${NEXUS_JWT_SECRET:-ci-not-for-prod}"

# Crown-jewel never-green-wash logic that MUST be present + green (never a silent
# skip): the grounded value oracle and the regression/outcome oracle.
REQUIRED=(test_value_oracle.py test_outcome_oracle_breadth.py)

mapfile -t FILES < <(ls tests/test_*.py 2>/dev/null | sort)
[ "${#FILES[@]}" -gt 0 ] || { echo "FATAL: no platform/api tests discovered." >&2; exit 1; }

echo "== platform/api suite: ${#FILES[@]} files, per-file isolation =="
fail=0; failed=()
for f in "${FILES[@]}"; do
  # -rfEX, NOT -rX. `-r` REPLACES pytest's default summary selection, so `-rX`
  # asked for xpassed ONLY — which silently switched OFF the `FAILED …` lines
  # that the grep below exists to surface. The result: a failing file printed its
  # count ("2 failed, 6 passed") and then nothing at all, so CI reported THAT a
  # file broke while withholding WHICH test broke and why. Observed on
  # test_module_graph_smoke.py, which is green locally and red on the runner —
  # precisely the case where the traceback is the only way in.
  out="$(python -m pytest "$f" -p no:cacheprovider --no-header -q --tb=short -rfEX 2>&1)"; rc=$?
  summary="$(printf '%s\n' "$out" | grep -E '[0-9]+ (passed|failed|error|xfailed|xpassed|skipped)' | tail -1)"
  printf '  %-52s %s\n' "$(basename "$f")" "${summary:-<no tests collected>}"
  if [ "$rc" != "0" ]; then
    fail=1; failed+=("$(basename "$f")")
    # The FULL output for a failing file, not a grep of it. A one-line `FAILED`
    # names the test but not the cause, and a runner-only failure cannot be
    # reproduced locally by definition — the traceback has to survive the run
    # that produced it. Only failing files pay this cost; green files stay one
    # line each.
    printf '%s\n' "$out" | sed 's/^/      /'
  fi
done

# Guard: the crown-jewel files must exist (a rename/move must not silently drop them).
for r in "${REQUIRED[@]}"; do
  [ -f "tests/$r" ] || { echo "FATAL: required crown-jewel test $r is missing." >&2; fail=1; }
done

echo ""
if [ "$fail" = "0" ]; then
  echo "PLATFORM_API_TESTS: PASS — every file green in isolation (known regressions xfailed)."
else
  echo "PLATFORM_API_TESTS: FAIL — ${failed[*]:-<crown-jewel guard>}" >&2
fi
exit "$fail"

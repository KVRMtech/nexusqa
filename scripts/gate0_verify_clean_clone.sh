#!/usr/bin/env bash
# GATE 0 — "the repository builds successfully from a clean clone".
#
# Clones THIS repository's HEAD into a throwaway directory and proves the
# working tree is not load-bearing: that every file the suites need is in
# version control, and that a second party starting from the commit alone gets
# the same answer.
#
# Deliberately clones from the LOCAL repo rather than the remote: the point
# under test is "is the COMMIT complete", not "can I reach GitHub". A file that
# exists only in the author's working tree fails here exactly as it would fail
# for a reviewer.
set -uo pipefail

SRC="${1:-$(git rev-parse --show-toplevel)}"
DEST="${2:-${TMPDIR:-/tmp}/gate0-cleanclone}"

rm -rf "$DEST"
echo "== cloning $SRC -> $DEST"
git clone --quiet --no-hardlinks --single-branch "$SRC" "$DEST" || exit 2
cd "$DEST" || exit 2

echo "== HEAD: $(git rev-parse HEAD)"
echo "== clone tree status (must be clean):"
git status --porcelain | head -20
CLONE_DIRTY=$(git status --porcelain | wc -l)
echo "   dirty entries in a FRESH clone: $CLONE_DIRTY  (must be 0 — anything here"
echo "   is a file git normalises on checkout, i.e. a line-ending or filter bug)"

FAIL=0

# ── 1. every Python file compiles (ci.yml `compile` job) ──
echo ""
echo "== compile: every .py in the clone"
BAD=0
while IFS= read -r f; do
  python -m py_compile "$f" 2>/dev/null || { echo "   FAIL: $f"; BAD=1; }
done < <(find . -name '*.py' -not -path './.venv/*' -not -path './node_modules/*' -not -path './alembic/*')
[ "$BAD" = 0 ] && echo "   all compile OK" || FAIL=1

# ── 2. ruff, exactly as ci.yml `lint` runs it ──
echo ""
echo "== lint: ruff check . (from Nexus_power/, per ci.yml defaults.run)"
( cd Nexus_power && python -m ruff check . ) || FAIL=1

# ── 3. the engine lane, exactly as ci.yml `qe-explorer-tests` runs it ──
echo ""
echo "== engine lane: pytest tests --ignore=tests/browser"
( cd Nexus_power/engines/qe-explorer \
  && python -m pytest tests --ignore=tests/browser -q --tb=short \
       -p no:cacheprovider -p no:randomly 2>&1 | tail -5 ) || FAIL=1

# ── 4. the characterization goldens, from the clone's own committed bytes ──
echo ""
echo "== characterization goldens (the A2 subject), TWICE for determinism"
for pass in 1 2; do
  printf "   pass %s: " "$pass"
  ( cd Nexus_power/engines/qe-explorer \
    && python -m pytest tests/test_characterization.py -q --tb=line \
         -p no:cacheprovider -p no:randomly 2>&1 | grep -E "passed|failed" | tail -1 ) || FAIL=1
done

# ── 5. a golden must not be rewritten by running the suite (CI's own guard) ──
echo ""
echo "== did running the suite rewrite a committed golden or the evidence?"
if git diff --quiet -- Nexus_power/engines/qe-explorer/tests/characterization/goldens \
                       Nexus_power/engines/qe-explorer/tests/browser/golden \
                       Nexus_power/evidence; then
  echo "   no — goldens and evidence are byte-identical after the run"
else
  echo "   YES — a run mutated committed evidence:"
  git diff --stat -- Nexus_power/engines/qe-explorer/tests/characterization/goldens \
                     Nexus_power/engines/qe-explorer/tests/browser/golden \
                     Nexus_power/evidence
  FAIL=1
fi

echo ""
if [ "$FAIL" = 0 ] && [ "$CLONE_DIRTY" = 0 ]; then
  echo "CLEAN_CLONE: PASS"
else
  echo "CLEAN_CLONE: FAIL (FAIL=$FAIL CLONE_DIRTY=$CLONE_DIRTY)"
fi
exit "$FAIL"

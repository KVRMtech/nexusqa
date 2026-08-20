#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# GATE 0 / A5 — make BOTH CI lanes mandatory before merge.
#
#   bash scripts/gate0_require_ci_lanes.sh              # DRY RUN (default)
#   bash scripts/gate0_require_ci_lanes.sh --apply      # actually change protection
#   bash scripts/gate0_require_ci_lanes.sh --verify     # print current state and exit
#
# WHY A SCRIPT AND NOT A CLICK-PATH. Branch protection is the one part of this
# programme with no diff and no review. A README step gets done from memory,
# half, once. This is idempotent, prints what it will change before changing it,
# and refuses to run when the preconditions are not met.
#
# ── WHAT IT ADDS ───────────────────────────────────────────────────────────
#   integrity-proof                                  (ci.yml)
#   jsdom execution lane                             (browser-harness.yml)
#   Chromium lane + characterization + coverage      (browser-harness.yml)
#
# `integrity-proof` is the job M1.7 shipped and documented as BLOCKING — the one
# whose stated purpose is destroying claims that outrun their evidence. It has
# been advisory since the day it was written.
#
# ── WHAT IT DELIBERATELY DOES NOT ADD ──────────────────────────────────────
#   Crawl acme-life / Crawl summit-life-carrier / Crawl vkpower-life
#
# The three proving-ground matrix jobs. Requiring a check whose green-ness has
# never been measured blocks every merge on the strength of a guess. That is
# Closure Plan A17 (Gate 2), and folding it into A5 would be exactly the
# over-claim this gate exists to stop. Add them there, on evidence.
#
# ── PRECONDITIONS, CHECKED BELOW ───────────────────────────────────────────
#   1. gh is authenticated with `repo` scope.
#   2. Each context being added has REPORTED at least once on a recent commit.
#      A required status check that has never run is never reported, and GitHub
#      blocks the pull request forever waiting for it. That is not a gate, it is
#      an outage — and it is the exact trap the `paths:` filter on
#      browser-harness.yml's pull_request trigger would have sprung (removed in
#      commit 0a91cea for this reason).
#
# ── AFTER APPLYING: PROVE THE REFUSAL ──────────────────────────────────────
# Enforcement is not evidenced by configuration, only by a refused merge:
#
#   git switch -c gate0/prove-refusal
#   # break exactly one required lane, e.g. a deliberate ruff violation
#   printf 'import os\nimport os\n' >> Nexus_power/engines/qe-explorer/app/emit.py
#   git commit -am 'PROOF ONLY — do not merge' && git push -u origin HEAD
#   gh pr create --base develop --title 'Gate 0 refusal proof' --body 'Do not merge.'
#   gh pr merge --merge          # MUST fail with 405 "required status checks"
#
# Record the 405. Then close the PR and delete the branch.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

REPO="${GATE0_REPO:-KVRMtech/nexusqa}"
BRANCH="${GATE0_BRANCH:-develop}"
MODE="${1:---dry-run}"

ADD=(
  "integrity-proof"
  "jsdom execution lane"
  "Chromium lane + characterization + coverage"
)

command -v gh >/dev/null || { echo "FATAL: gh not on PATH" >&2; exit 2; }
# `gh auth status` exits non-zero if ANY configured account has a stale token,
# even when the ACTIVE one is fine -- this box has a second, expired account and
# the check refused a perfectly usable session. Probe the API instead: the only
# question that matters is whether this token can read the repository.
gh api "repos/${GATE0_REPO:-KVRMtech/nexusqa}" --jq .full_name >/dev/null 2>&1 || {
  echo "FATAL: gh cannot read ${GATE0_REPO:-KVRMtech/nexusqa} — check 'gh auth status'" >&2
  exit 2; }

echo "== repository : $REPO"
echo "== branch     : $BRANCH"
echo

CUR_JSON="$(gh api "repos/$REPO/branches/$BRANCH/protection/required_status_checks" 2>/dev/null)" || {
  echo "FATAL: cannot read protection for $BRANCH. Either it is unprotected (in" >&2
  echo "which case adding required checks is not the first problem) or the token" >&2
  echo "lacks admin:repo. Refusing to guess." >&2
  exit 2
}

mapfile -t CURRENT < <(printf '%s' "$CUR_JSON" | python -c \
  'import json,sys; [print(c) for c in json.load(sys.stdin).get("contexts",[])]')

echo "-- currently required (${#CURRENT[@]}):"
printf '     %s\n' "${CURRENT[@]}"
echo

MISSING=()
for c in "${ADD[@]}"; do
  found=0
  for e in "${CURRENT[@]}"; do [ "$e" = "$c" ] && found=1 && break; done
  [ "$found" = 0 ] && MISSING+=("$c")
done

if [ "${#MISSING[@]}" -eq 0 ]; then
  echo "Nothing to do — all three contexts are already required."
  exit 0
fi

echo "-- would ADD (${#MISSING[@]}):"
printf '     + %s\n' "${MISSING[@]}"
echo

# ── precondition 2: has each context ever reported? ──
echo "-- checking each context has reported on a recent commit (the never-runs trap):"
SEEN="$(gh api "repos/$REPO/commits/$BRANCH/check-runs?per_page=100" \
        --jq '.check_runs[].name' 2>/dev/null | sort -u)"
UNSEEN=()
for c in "${MISSING[@]}"; do
  if printf '%s\n' "$SEEN" | grep -qxF "$c"; then
    echo "     ok       $c"
  else
    echo "     NEVER RAN  $c"
    UNSEEN+=("$c")
  fi
done
echo

if [ "${#UNSEEN[@]}" -gt 0 ]; then
  echo "REFUSING: ${#UNSEEN[@]} context(s) have not reported on $BRANCH's head commit." >&2
  echo "Requiring them now would block every pull request indefinitely, because" >&2
  echo "GitHub waits for a status that never arrives. Push a commit that runs both" >&2
  echo "workflows, let them finish, then re-run this script." >&2
  echo "" >&2
  echo "Override only if you know the workflow ran on a DIFFERENT branch:" >&2
  echo "    GATE0_SKIP_SEEN_CHECK=1 bash $0 --apply" >&2
  [ "${GATE0_SKIP_SEEN_CHECK:-}" = "1" ] || exit 3
  echo "GATE0_SKIP_SEEN_CHECK=1 set — proceeding anyway." >&2
fi

if [ "$MODE" = "--verify" ]; then exit 0; fi
if [ "$MODE" != "--apply" ]; then
  echo "DRY RUN — nothing changed. Re-run with --apply to make it so."
  exit 0
fi

# PATCH, not PUT: PUT on .../protection replaces the ENTIRE protection object,
# so a PUT that forgets enforce_admins or required_pull_request_reviews silently
# TURNS THEM OFF. The required_status_checks sub-resource takes a PATCH that
# touches only the contexts list.
NEW_CONTEXTS="$(printf '%s\n' "${CURRENT[@]}" "${MISSING[@]}" | python -c \
  'import json,sys; print(json.dumps([l.rstrip("\n") for l in sys.stdin if l.strip()]))')"
STRICT="$(printf '%s' "$CUR_JSON" | python -c 'import json,sys; print(str(json.load(sys.stdin).get("strict",False)).lower())')"

echo "-- PATCHing required_status_checks …"
printf '{"strict":%s,"contexts":%s}' "$STRICT" "$NEW_CONTEXTS" \
  | gh api --method PATCH "repos/$REPO/branches/$BRANCH/protection/required_status_checks" \
      --input - > /dev/null || { echo "FATAL: PATCH failed" >&2; exit 4; }

echo "-- now required:"
gh api "repos/$REPO/branches/$BRANCH/protection/required_status_checks" \
  --jq '.contexts[]' | sed 's/^/     /'
echo
echo "APPLIED. Enforcement is NOT yet evidenced — run the refusal proof in the"
echo "header of this script and record the 405."

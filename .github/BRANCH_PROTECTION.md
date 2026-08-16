# Branch protection — required configuration (M0.1 / T-CI-05)

**Status: NOT APPLIED. This requires repository-admin rights that CI and the
current tooling account do not hold.**

CI can only *report* a failure. What stops a red build from reaching `develop`
is a repository setting, and a setting is not code — so this file is the exact,
runnable specification of what an admin must apply, kept next to the workflow
whose checks it names.

---

## Why this is not "done" in code

The status checks below are produced by [`workflows/ci.yml`](./workflows/ci.yml)
and are real the moment a PR opens. Nothing in this repository can make them
*required* — GitHub enforces merges, and enforcement is configured through the
repository's branch-protection API by an account with `admin` permission.

Verified on 2026-08-16 against `KVRMtech/nexusqa`:

```
$ gh api repos/KVRMtech/nexusqa --jq '.permissions'
{"admin":false,"maintain":false,"pull":true,"push":false,"triage":false}

$ gh api repos/KVRMtech/nexusqa/branches/develop/protection
{"message":"Not Found","status":"404"}          # ← no protection today

$ gh api repos/KVRMtech/nexusqa/rulesets
[]                                              # ← and no rulesets either
```

So today **a PR with every check red can still be merged into `develop`.**
Applying the commands below is the step that closes it.

---

## The required status checks

These strings must match the CI check names EXACTLY. They are the job IDs from
`ci.yml` (a job with no `name:` reports under its ID), which is why the new jobs
deliberately carry no display name — a check name is an identifier that a
protection rule depends on, not prose to be reworded later.

| Check | What it gates |
|---|---|
| `lint` | ruff lint + format across the tree |
| `compile` | every `.py` file compiles |
| `test` | root `tests/` suite with coverage |
| `frontend` | client tsc + lint + vitest + build |
| `platform-api-tests` | platform/api suite (per-file isolation) |
| `qe-central-tests` | qe-central pure-logic suite |
| `qe-explorer-tests` | crawl engine, non-browser (923 tests, ~1 min) |
| `qe-explorer-browser` | the capture JS in jsdom + real Chromium (~12 min) |
| `qe-explorer-characterization` | golden-snapshot crawls; a diff = behaviour changed (~11 min) |
| `harness-jsdom` | the injected `inventory_js` walker under jsdom |
| `QE-Central database & tenant-isolation contract` | RLS / migration / reaper against real Postgres |
| `crawl-smoke` | one real crawl produces a manifest with pages > 0 |

> The database check is the one job that carries a prose `name:`; it predates
> this milestone and its display name is what GitHub reports, so it is quoted
> here verbatim. Do not "tidy" it without updating the protection rule in the
> same change — renaming a required check silently makes the requirement
> unsatisfiable, and every PR blocks on a check that will never report.

`docker-build` and `security` are intentionally **not** required: the first is
long and only meaningful post-merge, and the second reports third-party CVE
drift that is not the PR author's to fix. Making either required would train
people to bypass protection, which is worse than not having it.

---

## Apply it (admin)

Run once per protected branch. `develop` is the default and integration branch,
so it matters most; `main` is included so the rule already exists the day a
release branch does.

```bash
for BRANCH in develop main; do
  gh api -X PUT "repos/KVRMtech/nexusqa/branches/${BRANCH}/protection" \
    --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "lint",
      "compile",
      "test",
      "frontend",
      "platform-api-tests",
      "qe-central-tests",
      "qe-explorer-tests",
      "qe-explorer-browser",
      "qe-explorer-characterization",
      "harness-jsdom",
      "QE-Central database & tenant-isolation contract",
      "crawl-smoke"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
done
```

`main` does not exist on the remote yet; the call for it will 404 until the
branch is created. That is expected — create the branch first, or run the
`develop` half alone.

### The choices worth knowing about

* **`strict: true`** — a PR must be up to date with the base before merging.
  It is what stops two individually-green PRs from merging into a broken
  combination. It costs a rebase on a busy branch; this repository is not busy
  enough for that to hurt.
* **`enforce_admins: false`** — admins can still merge past a red check. Left
  off deliberately: this repository has a single-digit contributor count and a
  self-hosted deploy path, and locking the only admin out of an emergency fix is
  a worse failure than the one being prevented. Turn it on once there is a
  second admin.
* **`allow_force_pushes: false` / `allow_deletions: false`** — these matter more
  than they look. CI evidence is only trustworthy if the commit it ran against
  is still the commit that got merged.
* **`required_conversation_resolution: true`** — an unresolved review thread
  should block a merge; it is the cheapest of these to satisfy.

---

## Verify it (this is the acceptance evidence)

```bash
gh api repos/KVRMtech/nexusqa/branches/develop/protection \
  --jq '{checks: .required_status_checks.contexts,
         strict: .required_status_checks.strict,
         reviews: .required_pull_request_reviews.required_approving_review_count}'
```

Then prove enforcement rather than assuming it — the milestone asks for a
failing check to actually block a merge:

1. Branch from `develop`, break one unit test on purpose, open a PR.
2. Confirm the corresponding check goes red.
3. Confirm the merge button is blocked, and that `gh pr merge` refuses.
4. Revert the break, confirm the checks go green and the merge is allowed.

Until step 3 has been observed, this task is **CODE READY — GITHUB ADMIN
CONFIGURATION STILL REQUIRED**, not complete.

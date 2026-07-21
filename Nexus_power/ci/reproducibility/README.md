# Reproducibility gate

**Goal (Phase 0 of the production plan): git must reproduce exactly what runs.**
A security team — or a fresh CI runner — must be able to clone the repo, build, and
get the *same* system. The failure mode this closes is silent drift: a module that
lives in git, compiles, even has tests, yet is wired into **nothing** the running
service imports — so a clean build omits it (or a hand-patched server runs code git
orphans). A parse-only CI never sees this.

## What `reachability_gate.py` does

Statically (no code executed) it follows each service's **real entrypoint** — taken
from the Docker `CMD`/`ENTRYPOINT` — through intra-service imports, and reports every
`.py` that is reachable from nothing. It over-approximates reachability, so a flagged
module genuinely has zero inbound references; there are no false orphans from an
ambiguous import.

It is standard-library only, deterministic, and offline. Nothing about this repo is
hardcoded: services are auto-discovered from their Dockerfiles; exclusions are path
*conventions* (`tests/`, `alembic*/`, migrations, vendored dirs), never module names.

## Run it

```bash
# one service
python ci/reproducibility/reachability_gate.py --source-root platform/qe-central --root app.main

# whole repo; make the Verdict services blocking, everything else report-only
python ci/reproducibility/reachability_gate.py --discover . \
  --gate 'platform/api' --gate 'platform/qe-central' --gate 'platform/gateway' \
  --gate 'platform/auth-service' --gate 'engines/qe-explorer' --gate 'engines/repo-intel'
```

Exit code is non-zero when a **gated** service has an orphan or a missing entrypoint.
CI runs exactly this (`.github/workflows/ci.yml` → `reachability` job).

## Resolving an orphan — the only two allowed moves

For each entry in [`DRIFT_BACKLOG.md`](./DRIFT_BACKLOG.md):

1. **Wire it** into the service at its real integration point. For the frozen
   `platform/api` factory, the running container is canonical — capture the wiring
   *from what actually runs*, don't invent an integration point.
2. **Remove it** from git if the running system genuinely does not use it (dead or
   superseded code).

**Never** add an ignore list / baseline to make the gate pass. That reintroduces the
exact drift it exists to catch — the CI reads the live tree, so a suppression is a
lie, not a fix. The gate is green only when git and the running system agree.

## Tests

`test_reachability_gate.py` builds throwaway package trees and asserts the gate's
judgments (orphan flagged, wired kept, tests/migrations excluded, entrypoint parsing).
Run: `pytest ci/reproducibility/test_reachability_gate.py`.

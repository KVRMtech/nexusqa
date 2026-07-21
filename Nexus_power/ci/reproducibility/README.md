# Reachability / dead-code gate

**What this is:** a static gate that finds shipped `.py` files wired into **nothing**
a running service imports — code that compiles and may even have tests, yet is
reachable from no entrypoint. That is either **latent code** (a feature built but
never activated) or **dead code** (superseded). Both are cleanup debt on the road to
a 9.5/10 production bar, and a parse-only CI never sees them.

**What this is NOT (important, measured 2026-07-21):** it is *not* a git-vs-deployed
reproducibility check, and the modules it flags are *not* evidence that git differs
from what runs. A direct file-hash diff of git `platform/api` against the running VM
found them **162/164 identical** (the 2 differences: one is line-endings only; the
other is a file where *git is ahead*). So git reproduces the frozen factory faithfully.
The real reproducibility work (Phase 0) is deploy discipline — rebuild the running box
from CI images so its *baked dependencies* match git — tracked separately in
`docs/HARDENING_SUMMITLIFE_HANDOVER_2026-07-21.md`. This gate is the complementary
hygiene check: keep the tree free of code that runs from nowhere.

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

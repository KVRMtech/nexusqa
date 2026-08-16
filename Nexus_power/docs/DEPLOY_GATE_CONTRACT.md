# The Deploy Gate Contract (M0.4)

The operational contract for `scripts/deploy.ps1` and the golden-crawl gate.
This file is the authority for **exit codes**, the **rollback decision matrix**,
and **which files a gate run may write**. If code and this document disagree,
`Nexus_power/tests/deploy_gate/test_pipeline_contract.py` is the tiebreaker —
it asserts these invariants against the scripts themselves.

---

## 1. Deployment flow

```
  push
    ↓
  ┌─────────────────────────┐
  │ PREFLIGHT  host_health  │──── unhealthy ──▶ ABORT (exit 2)
  │ (pre-swap)              │                   nothing swapped,
  └─────────────────────────┘                   previous build still serving
    ↓ healthy
  BUILD  images on the VM
    ↓
  SWAP   docker compose up -d --force-recreate
    ↓
  MANIFEST  .deploy_manifest.json  ← the deployment inventory, written once
    ↓
  ┌─────────────────────────┐
  │ GATE  golden_crawl_gate │
  │  host health (again)    │
  │  dispatch a real crawl  │
  │  read the funnel        │
  │  ratchet vs baseline    │
  │  catalog completeness   │
  └─────────────────────────┘
    ↓
  ROLLBACK DECISION  (§3)
    ↓
  FINALIZE  record HEAD as .last_green_deploy
```

The gate runs **after** the swap — a real crawl needs the new build serving. So
"the gate blocks the deploy" can only honestly mean: *the fleet does not stay on
a build the gate refused.* The pre-swap preflight exists so the most common
infrastructure failure is caught while the previous build is still serving, and
nothing has to be reverted at all.

---

## 2. Gate exit codes

The gate's **last line of output** is always `GATE_VERDICT=<verdict>`, emitted by
the same statement as the exit code so the two cannot drift. A caller whose SSH
dropped before the exit code arrived can still read the verdict.

| Code | Verdict            | Meaning                                                        |
|------|--------------------|----------------------------------------------------------------|
| 0    | `PASS`             | No funnel regression.                                           |
| 1    | `APP_UNHEALTHY`    | Host was healthy; the **build** could not produce a crawl.      |
| 2    | `USAGE`            | The gate was invoked wrongly. No verdict.                       |
| 3    | `REGRESSION`       | A funnel metric or the catalog went backwards.                  |
| 4    | `HOST_UNAVAILABLE` | Infrastructure could not support a verdict. **Not** a verdict.  |

`HOST_UNAVAILABLE` covers: disk ≥90%, ≥100 exited containers, a required
container down, an unreadable exploration row, an uncountable catalog table.

---

## 3. Rollback decision matrix

**Rollback follows deployment correctness, never monitoring availability.**

| Condition                              | Verdict            | Rollback | Deploy exit |
|----------------------------------------|--------------------|----------|-------------|
| Funnel or catalog regressed            | `REGRESSION`       | **YES**  | 1           |
| Deployed app could not produce a crawl | `APP_UNHEALTHY`    | **YES**  | 1           |
| Container swap itself failed           | —                  | **YES**  | 1           |
| Host unhealthy (disk, dead container)  | `HOST_UNAVAILABLE` | **NO**   | 2           |
| Monitoring/DB unreadable               | `HOST_UNAVAILABLE` | **NO**   | 2           |
| SSH dropped / no verdict line          | *(none)*           | **NO**   | 2           |
| Gate passed                            | `PASS`             | **NO**   | 0           |

Why the "NO" rows are not a loophole: an infrastructure failure means we know
**less** about the build, not that the build is bad. Reverting on it is an outage
we inflict on ourselves — and it happened: a full disk on the VM once reverted a
deployment nothing had found fault with. Every "NO" row still exits **non-zero**
and prints `UNVERIFIED`; the build is not blessed, it is merely not reverted, and
`.last_green_deploy` is left untouched so the next deploy still has a true anchor.

---

## 4. Rollback

One implementation: `scripts/gate_rollback.sh`. `deploy.ps1` calls it on a red
gate, and `gate_rollback_drill.sh` drills that same executable — so the drill
exercises the code that runs during an incident, not a copy of it.

* **Input** is `.deploy_manifest.json`, written at deploy time from an inventory
  captured once, before anything is built. Rollback targets are data, never
  re-derived from a variable at rollback time.
* **Order** is the reverse of deploy order (LIFO); `platform-api` is restored
  first, so the backend is back before its callers.
* **All-or-report**: every service in the manifest must come back. A partial
  restore exits 1, prints `ROLLBACK INCOMPLETE`, and **names the services still
  on the rejected build**.
* **No inventory, no rollback**: a missing/corrupt/unversioned manifest, or no
  `.last_green_deploy` anchor, exits 2 without touching a container. Restoring an
  arbitrary subset is worse than refusing.

```
gate_rollback.sh --src <repo> [--manifest <path>] [--green <sha>] [--dry-run]
  0  every deployed service restored
  1  one or more NOT restored — the fleet is MIXED, intervene
  2  could not attempt — no manifest or no green anchor
```

---

## 5. What a gate run may write

| File                                        | Tracked | Written by                          |
|---------------------------------------------|---------|-------------------------------------|
| `scripts/golden_crawl_baseline.json`        | **yes** | `--update-baseline` / `--rebaseline` **only** |
| `scripts/.gate_runtime_state.json`          | no      | every gate run (gap bookkeeping)    |
| `.deploy_manifest.json`                     | no      | every deploy                        |
| `.last_green_deploy`                        | no      | a green gate                        |

**A plain gate run leaves the working tree clean.** Gap bookkeeping used to be
written into the tracked baseline on every run, which dirtied the tree and turned
the next `git pull` on the VM into a merge conflict over a counter no reviewer can
act on. Override the runtime state location with `$GOLDEN_GATE_STATE`.

Floors move only by explicit operator command:

```
golden_crawl_gate.sh <app> --update-baseline        # RAISE only, clean runs only
golden_crawl_gate.sh <app> --rebaseline "why"       # may LOWER; records the reason
golden_crawl_gate.sh <app> --exploration <id>       # re-evaluate recorded evidence
```

---

## 6. The ratchet

`scripts/gate_baseline.py` owns the metric list. **Adding a floor means adding it
to `RATCHETED_METRICS` and nowhere else** — the checker, the raise writer and the
rebaseline writer all derive from that one tuple. The list previously existed in
three hand-maintained copies, and two metrics (`selects_filled`,
`forms_confirmed`) were evaluated by the checker while being absent from both
writers, so their floor stayed 0 forever and neither could ever regress.

Per-metric verdicts:

* **FAIL** — below the best ever seen. Blocks.
* **RISE** — above it. The floor moves up on the next `--update-baseline`.
* **GAP** — never achieved (floor 0, current 0). **Not a pass.** Tolerated for
  `GOLDEN_GAP_MAX_RUNS` (3) runs / `GOLDEN_GAP_MAX_DAYS` (7) days, then RED.
* **OK** — holds at the floor.

A **corrupt** baseline fails the gate; a **missing** one is a legitimate first
run. Collapsing both to "no floors" would make every metric rise above zero and
pass everything — green-wash through the gate's own front door.

### Catalog completeness

`catalog_questions` = `SELECT count(*) FROM catalog_questions WHERE app_id=…`.
The Master Catalog is the product's deliverable; every other floor measures the
crawl. A fold change that drops a question class, or a dedup key that collapses
distinct questions, shrinks the catalog while pages/forms/flows all hold.

> **Operational note.** `catalog_questions` ships as a new, unmet floor. Its first
> real gate run RISEs it to the observed count and it self-enforces from then on.
> If a crawl genuinely produces zero catalog questions it will be tolerated for
> three runs and then turn the gate red — which is the intended news.

---

## 7. Drills

| Script                  | Proves                                                            |
|-------------------------|-------------------------------------------------------------------|
| `gate_canary.sh`        | A regressed floor turns the gate RED (exit 3) **and only then**.  |
| `gate_gap_proof.sh`     | A never-met floor is never green, and goes RED once overdue.      |
| `gate_rollback_drill.sh`| Multi-service rollback: full set, LIFO order, partial = failure, baseline untouched. |

All three replay a recorded exploration (`--exploration`), so they cost no crawl
and take no single-flight lock. Each uses an isolated `$GOLDEN_GATE_STATE`: run
against the host's real gap state, a routine canary would age every unmet floor
three runs closer to overdue and eventually turn the gate red by being run.

---

## 8. Verification report fields

`lint_spec` (`playwright_auditor.LINT_RULES_VERSION`) is a real, deterministic
API-policy lint. Reports carry `lint`, `lint_errors`, `lint_status: "executed"`
and `lint_rules_version`, because `"lint": []` alone cannot be told apart from
"the lint never ran" — and for months it meant exactly that.

`error` severity is reserved for API usage the compiler provably never emits
(`waitForTimeout`, `page.<action>(selector)`, ElementHandles, un-awaited retrying
matchers), so compiler-generated specs score zero errors and their risk scores are
unchanged. Deliberate compiler idioms (bounded `networkidle`, the visual
coordinate fallback, tolerant `.catch()` oracles) are `warning`: surfaced, never
scored.

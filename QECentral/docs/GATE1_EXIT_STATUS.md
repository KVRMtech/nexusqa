# Gate 1 — Exit Status

**Date:** 2026-08-21 · **Branch:** `feat/qec-dynamic-catalog-p0-p6`

Nine of the ten Gate 1 exit criteria are met. The tenth — *"A6–A10 merged into
the primary branch"* — **cannot be met as written**, for a structural reason
that has nothing to do with Gate 1's readiness. That reason is documented below
with the commands that establish it, because the same obstacle blocks every
other gate on this branch and is currently undocumented.

---

## 1. Criteria met

| Criterion | Status | Evidence |
| --- | --- | --- |
| Radio-group unblock implemented | ✅ | `walker.py` / `forms.py` / `matcher.GROUP_ASSEMBLE` — **LIVE-PROVEN, see §5** |
| Radio-group validation suite | ✅ | `tests/test_answer_to_unblock_radio.py` |
| Honest adjudication prevents false completion | ✅ | `crawler._journeys_walked`, incremented at the two WALK-path append sites |
| Chromium validates same-page wizard progression | ✅ | fixture **27** `27-wizard-20-step-samefingerprint` |
| Live repair within configured retry budget | ✅ | `forms.py` `repairable` wiring |
| Provisioning-proof issuer operational | ✅ | `A11_ATTESTATION_ISSUER.md` |
| **Independent second-squad certification** | ✅ | `A11_INDEPENDENT_CERTIFICATION.md` — certified with findings, re-affirmed against a commit |
| WALK persistence for trusted environments only | ✅ | `A12_WALK_PERSISTENCE.md` — 7 tests, real Chromium |
| F10 formally reconciled | ✅ | `qe-explorer/docs/GATE1_F_REGISTER.md` — VOID, never issued |
| CI passes without regression | ✅ | see §3 — full browser lane 600 passed, 0 failed |

**Two premises in the Gate 1 brief were factually wrong** and would have
produced the wrong work if taken at face value:

* *"Chromium Fixture 22"* — slot 22 is `22-collapsed-disclosure`. The 20-step
  same-shape wizard is fixture **27**.
* *"Repair exhausted after one attempt though `RepairBudget.attempts = 3`"* —
  never a retry-count problem. `RepairBudget` had defaulted to 3 since T-FE-01;
  the real cause was `repairable = provenance == PROV_SYNTHESIZED` making
  `_regenerate` return `None` instantly.

---

## 2. The merge criterion cannot be satisfied as written

### 2.1 "A6–A10" is not a separable merge unit

```
$ git rev-list --count develop..HEAD          ->  55
$ git diff --name-only develop...HEAD | wc -l ->  429
$ git log --format="%s" develop..HEAD | ...   ->  gate0, gate1, gate2, gate3, gate4
```

The branch is the shared trunk for **five gates and roughly nine concurrent
sessions**. Gate 1's own commits are a small minority of the 55. There is no
commit range that is "A6–A10": Gate 1 work is interleaved with Gate 2, 3 and 4
work from other squads, much of it uncertified and some of it explicitly
in-flight.

**Merging this branch does not merge A6–A10. It merges everyone's work,
including squads who have not signed off.**

### 2.2 The primary branch of record is not a merge target

```
$ git merge-base develop HEAD          ->  ede6bf2…      (shares history)
$ git merge-base origin/develop HEAD   ->  (empty)       NO COMMON ANCESTOR
```

| | merge-base | top-level layout |
| --- | --- | --- |
| local `develop` | `ede6bf2` | `Nexus_power/`, `QECentral/` |
| `origin/develop` | **none** | application at repository **root** |

`origin/develop` is a single flattened "Initial commit" that shares no history
with the working branches and carries the application at a different path.
GitHub refuses to open a PR against it at all — which is why `.github/workflows/ci.yml`
carries a **bootstrap push trigger** with a comment stating the histories are
still unreconciled.

So the criterion resolves to one of two different things depending on which
`develop` is meant, and neither is a Gate 1 action:

* **local `develop`** — mechanically possible, but carries 5 gates of other
  squads' work to the trunk without their sign-off;
* **`origin/develop`** — blocked on a repository-history reconciliation that is
  open, owned by nobody in Gate 1, and explicitly flagged in CI config.

### 2.3 Decision

**Not merged, deliberately.** Gate 1's engineering is complete and verified;
the merge is a release-management action on a contended trunk whose target is
ambiguous and whose payload is mostly other squads' work. Performing it from
inside Gate 1 would repeat — at merge scale — the shared-index sweeping that
already mis-attributed one commit today.

**Recommended sequence**, for whoever owns release management:

1. Reconcile `origin/develop` with the working lineage, or formally designate
   local `develop` as the primary branch and record that decision.
2. Get CI green on the branch as a whole (§3), not per-gate.
3. Merge the trunk **once**, with all gate owners signed off — not gate by gate.

---

## 3. CI / regression status

| Suite | Result |
| --- | --- |
| qe-explorer non-browser | **2025 passed** |
| qe-central | **2296 passed**, 146 skipped |
| A11 author suites | **143 passed**, 0 skipped |
| A11 independent certification | 9/9 digests, **131 checks**, 1 known finding (CERT-FINDING-2) |
| A12 walk persistence (Chromium) | **7 passed** |
| Browser lane, targeted | **48 passed** |
| Browser lane, full (during concurrent golden rewrite) | 571 passed / **23 failed** — all `test_manifest_golden[...]` |
| Browser lane, full (**confirmation run, after those goldens landed**) | **600 passed, 0 failed**, 299 skipped, 3 xfailed — 1:12:11 |

**Confirmed: not a regression.** The confirmation run reproduces the same lane
with zero failures. The diagnosis below was established by experiment before
that run, and the run agrees with it:

1. `[18-select-edge-cases]` **passes alone**, fails in the lane → order/state
   dependent, not a stale golden.
2. Gate 1's new A12 module + that golden test → **8 passed** → the new module is
   not the polluter.
3. During that lane run, **29 golden manifests were being rewritten on disk** by
   a concurrent session.
4. After those goldens were committed, a re-run of characterization 16–22 —
   which includes 7 of the 23 failures — gave **18 passed**.

A shared-checkout race, not a defect — now closed by the confirmation run
above. It is worth naming the failure mode, because it will recur: a full lane
here takes ~70 minutes, and any session rewriting goldens during that window
makes the run read half-written files. A lane result is only trustworthy if
`git status -- .../tests/browser/golden/` was clean for its duration.

The 299 lane skips were checked and are principled: each fixture declares which
lanes can adjudicate it and states why the others cannot (no cross-origin
isolation in jsdom, no `innerText`, no canvas 2D context, no frame-locator).

---

## 4. Outstanding, carried forward

* **CERT-FINDING-2 (IPv6)** — `normalize_origin` strips IPv6 brackets and cannot
  re-parse its own output, so an IPv6 environment receives a valid signed proof
  that is guaranteed to be refused. Fix must land in **both** duplicated copies
  (`qe-explorer/app/attest.py`, `qe-central/app/services/walk_attestation.py`).
  It edits `attest.py`, which the certification snapshot pins, so it must land
  **together with re-certification**.
* **CERT-FINDING-1 (KMS rationale)** — the documented justification for holding a
  plaintext Ed25519 key in process heap is factually false; the decision needs
  re-taking on true grounds. Not a code change by itself.
* **Gate 1 is not DEPLOYED.** A6 is now live-proven (§5); A7–A13 are not. A12 is
  a local Chromium demonstration against a purpose-built application. Gate 2 is
  where the whole journey becomes actual.

---

## 5. A6 — LIVE-PROVEN on vkpowerlife (2026-08-21)

**A6's own acceptance criteria were *"vkpowerlife product-selection step
successfully traversed"* and *"journey advances beyond the product page"*. Both
are now met against the real deployment**, not a fixture and not the local
replica on `127.0.0.1:8101` that the earlier Gate 2 evidence used.

Target: `https://vkpowerlife.136-85-106-73.sslip.io/`
Instrument: `measure_radio_unblock.py` — asserts nothing, prints what came back.
Evidence: `Nexus_power/evidence/gate1/a6-vkpowerlife-live/coverage.json`

### The gate, and the experiment that cleared it

```
url      : /life-insurance/quote/start/
advance  : 'Continue'   reason = advance_disabled_by_app_validation
missing  : [Term Life, Whole Life, Universal Life, Variable Universal Life]   <- the radio group
answered : 'Term Life Insurance Affordable coverage for a specific period'
rule     : Continue requires an answer to 'product = ...' before it is enabled
           (proven: the app enabled it when the agent answered)
rule_reused : False        <- discovered on this run, not replayed
```

The app disabled `Continue`; the crawl discovered *why* by experiment, answered
the radio, and the app enabled it. That is the mechanism A6 was built for,
working on a real application.

### The journey advanced well beyond the product page

21 states, 15 advances (`{tier 1: 11, tier 3: 4}`), 12 distinct routes:

```
/  ->  /life-insurance/quote/start/   <- the gate
   ->  /quote/coverage/  ->  /quote/personal/  ->  /quote/health-check/
   ->  /quote/review/                <- stopped here
   /login/  /apply/member-lookup/  /apply/personal-info/  /apply/replacement/
   /portal/dashboard/  /portal/beneficiaries/
```

### What it did NOT do, which matters more

| | |
| --- | --- |
| `boundaries_crossed` | **0** |
| `forms_submitted` | **0** |
| `journeys_completed` | **0** — honestly reported, no false completion |
| `unblock_irreversible` | **0** — no experiment was left committed on the app |
| `network_server_error_count` | **0** — the crawl caused no 5xx |
| `approvable_boundary` | **1** — `Apply Now` at `/quote/review/`, `commit_shaped_label`, severity high |

The instrument deliberately withholds `boundary_approvals` and walk attestation
because this is a live deployment. The crawl therefore fills and advances and
**stops at the commit boundary**, which is where a measurement of *progression*
should stop. Crossing `Apply Now` would submit a real application and is a
separate, explicitly-approved decision.

Reproduced independently by a second run (the first, by another session, reached
6 advances / 5 routes; this one 15 advances / 12 routes — same gate, same rule,
same `rule_reused=False`).

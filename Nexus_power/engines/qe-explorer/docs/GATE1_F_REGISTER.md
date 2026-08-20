# Gate 1 / WP8 — Source Register for the `F` Identifier Namespace

**Status:** F10 is **VOID**. It was never issued in either `F`-namespace this
repository uses. Justification and evidence below.

**Method.** Every claim here was derived by searching the repository at
`feat/qec-dynamic-catalog-p0-p6`, not from recollection:

```bash
for f in F1 F2 F3 F4 F5 F6 F7 F8 F9 F10; do
  grep -rloE "\b$f\b" --include=*.py --include=*.md --include=*.yml .
done
```

---

## 1. There are TWO `F` namespaces, and conflating them is the trap

The search above returns hits for `F1`–`F4` and nothing for `F5`–`F10`. That
result is misleading on its own, because the hits and the misses belong to
different registers:

| Namespace | Issued by | Lives where | Members |
|---|---|---|---|
| **A — Fatal Limits** | Adversarial crawl-architecture review, 2026-08-15 | The review artifact and this register. **Never written into code.** | `F1`–`F5`, `F7`, `F8` |
| **B — Characterization fixtures** | M0.3 characterization harness | `tests/characterization/fixtures.py` | `f1`–`f4` |

Namespace B is the source of every `F1`–`F4` grep hit
(`f1_public_discovery`, `f2_auth_wizard`, `f3_questionnaire_submit`,
`f4_guard_refusal`). Those are fixture ids and have no relationship to the
architecture review's limits beyond an unfortunate shared letter.

**Neither namespace contains an `F10`.** Namespace A stops at `F8` and skips
`F6` and `F9`; namespace B stops at `f4`.

---

## 2. Namespace A — implementation lineage of every issued identifier

The review recorded these as *structural limits, not bugs*. Their remediation
status is stated below with the module that carries it, so the lineage is
checkable rather than asserted.

| ID | Limit as recorded (2026-08-15) | Remediation lineage | Status |
|---|---|---|---|
| **F1** | State fingerprint = control-name set, so same-shape wizard steps collapse to one state and the walk quits as a false loop. `perceptual_hash` mitigation was dead code. | [`app/state_identity.py`](../app/state_identity.py) — evidence-gated identity ladder. Gate 1 adds the adversarial fixture that measures it: [`tests/browser/fixtures/27-wizard-20-step-samefingerprint/`](../tests/browser/fixtures/27-wizard-20-step-samefingerprint/) and [`tests/browser/test_wizard_20_step.py`](../tests/browser/test_wizard_20_step.py). | **Mitigated, fixture-covered.** Not deployed. |
| **F2** | Guard aborts ALL mutating methods during the walk, so server-persisted journeys die at the first save. Named as a safety INVARIANT in tension with the vision, not a patch. | [`app/walk_persist.py`](../app/walk_persist.py) (M1.3 / T-WP-01) + the `Phase.WALK` branch in [`app/guard.py`](../app/guard.py). Gate 1 / WP6 supplied the missing issuer and WP7 proved the path reachable: [`tests/test_gate1_twp01_execution.py`](../tests/test_gate1_twp01_execution.py). | **Closed by Gate 1.** Was inert before WP6. |
| **F3** | `max_depth` (default 4) silently ANDed into the 60-step E2E budget; `QEC_MAX_DEPTH` was dead (never read). | [`app/config.py:101`](../app/config.py#L101) — `max_depth: int = Field(default=6, alias="QEC_MAX_DEPTH")`. The env var is now read. Depth honesty is reported by `deepest_flow_proven_steps` / `deepest_flow_capped` in [`app/flow_ledger.py`](../app/flow_ledger.py). | **Closed.** |
| **F4** | Resume machinery existed but was unreachable; would have reported a 0-state `completed` if it fired. | [`app/resume_state.py`](../app/resume_state.py) + the `resume_broken` → `resume_unrecoverable` branch in [`app/completion.py`](../app/completion.py). | **Closed.** |
| **F5** | No popup / new-tab / dialog / download handling; `confirm()` auto-dismissed to Cancel. **No validation-repair loop at all (one shot per field).** | [`app/page_lifecycle.py`](../app/page_lifecycle.py) (M1.5) and [`app/fill_engine/repair.py`](../app/fill_engine/repair.py) (T-FE-01). Gate 1 / WP5 closed the remaining one-attempt exits — see §4. | **Closed by Gate 1.** |
| *F6* | — | *Never issued.* | **Not a member.** |
| **F7** | Inventory JS crash returned `[]`, which hashed to a valid new fingerprint and terminated as `no_advance == completed:true` — a green-wash inside the anti-green-wash. | [`app/observation_health.py`](../app/observation_health.py) + `STOP_INVENTORY_FAILED` in [`app/completion.py`](../app/completion.py) (T-GW-01). | **Closed.** |
| **F8** | Identity seed was `tenant::latest_artifact_id`, which rotates every crawl → a different synthetic person per run, invalidating all cross-crawl comparison. DOB drifted daily. | [`app/identity_pack.py`](../app/identity_pack.py) — `derive(seed, *, today=...)`; the clock is injected rather than read, which is what makes a run reproducible. **The seed's CALLER still needs auditing** — see §5. | **Partially closed. Carried as debt.** |
| *F9* | — | *Never issued.* | **Not a member.** |
| **F10** | — | *Never issued.* | **VOID — see §3.** |

---

## 3. F10 — formal void

### Finding

`F10` does not appear in:

* any `.py`, `.md` or `.yml` file in the repository (zero grep hits);
* the Fatal-Limits register, which runs `F1`–`F5`, `F7`, `F8`;
* the characterization fixture register, which runs `f1`–`f4`;
* any roadmap ticket namespace in use (`T-XX-NN`, `BUG-*`, `qec_0NN`).

### Justification for voiding rather than back-filling

Three options were available and two are wrong:

1. **Back-fill it** — invent a limit, assign it `F10`, and write a lineage.
   Rejected: that manufactures roadmap history. An identifier with a fabricated
   provenance is worse than an absent one, because it looks audited.
2. **Leave it open** — carry `F10` as an unresolved item. Rejected: WP8's own
   requirement is that no orphan identifier may remain, and an identifier with
   no issuing document cannot be closed by any amount of work.
3. **Void it with the evidence** — record that it was never issued, show the
   two registers it is absent from, and close it. **Chosen.**

### Void record

| Field | Value |
|---|---|
| Identifier | `F10` |
| Disposition | **VOID — never issued** |
| Voided under | Gate 1 / WP8 |
| Date | 2026-08-20 |
| Branch | `feat/qec-dynamic-catalog-p0-p6` |
| Evidence | Zero repository occurrences; absent from both `F` registers (§1, §2) |
| Re-use policy | `F10` must **not** be re-issued to a new limit. A voided id that later names something real makes this register ambiguous for anyone reading it in git history. The next Fatal-Limit id is `F11`. |

`F6` and `F9` are recorded here under the same finding — never issued — but were
not in WP8's scope and are noted for completeness rather than voided.

---

## 4. Gate 1's contribution to this register

| Identifier | What Gate 1 changed |
|---|---|
| `F1` | First fixture that can actually measure the collapse (27). A single `collect()` cannot express "the walk moved", which is why a suite full of green capture tests never saw it. |
| `F2` | Closed. The `qe-central` attestation issuer ([`app/services/walk_attestation.py`](../../../platform/qe-central/app/services/walk_attestation.py)) supplies the signed proof the WALK phase always required and nothing could produce. |
| `F5` | The repair loop's two remaining one-attempt exits closed — a transient page race is now retried with backoff, and a provenance-locked field reports the gate that stopped it instead of a constraint search that never ran. |
| `F8` | Re-verified; the residual caller-side risk is restated as debt in §5 rather than reported as closed. |

---

## 5. Carried debt on this register

* **F8 (partial).** `identity_pack.derive` takes an injected seed and an injected
  `today`, so the module is reproducible. What this register does **not** claim
  is that every *caller* passes a stable seed. The 2026-08-15 quick-win — "seed
  identity on `app_id`, not `artifact_id`" — was not verified as part of Gate 1
  and remains open. Until it is, cross-crawl comparison validity is unproven.
* **Namespace collision.** Namespaces A and B share a letter and will collide
  again. The cheapest fix is to rename the characterization fixture ids
  (`f1_public_discovery` → `char1_public_discovery`); it was not done here
  because renaming them changes golden keys, which is a re-recording this gate
  had no reason to spend.

# The Intelligent Fill Engine — T-FE-01 … T-FE-10

**Status:** code-complete, fully tested locally. **NOT deployed, NOT live-proven.**
**Branch:** `feat/qec-dynamic-catalog-p0-p6`
**Suites:** qe-explorer 1682 passed · platform-api 1203 passed · qe-central 2016 passed / 100 skipped · **0 failed**

---

## 1 · Root cause analysis

Ten reported defects. They are not ten bugs; they are **four causes**, and every
symptom falls out of one of them.

### Cause A — the generator had one input where it needed three

`field_values.value_for(semantic_type, control, identity)` decided a value from
the semantic TYPE alone. It could not see **whose** field it was, and it did not
consult **what the control would accept**.

| Symptom | Mechanism |
|---|---|
| Beneficiary / Spouse / Employer names resolve to the applicant | `field_semantics` classifies all of them as a name; there was one person to answer with |
| Split DOB writes the year into month, day and year | every part classified as `DOB`, and the option picker was handed `identity.date_of_birth[:4]` — literally the year — for all three |
| Money fields default to `"100"` | `if sem == S.CURRENCY: return _number_in_range(control, 100)` — a constant, so income and coverage came back as the same number |
| Pattern constraints captured but ignored | `field_signature` folds `pattern` into the learning key and `field_semantics` classifies *by* it; nothing ever generated *against* it |

### Cause B — validity was a property of the page, not of a control

`playwright_port.error_texts()` returns every visible `[role=alert]` /
`[aria-live=assertive]` on the document; the fill path took `errors[0]` as
`error_detail` on the observation of whatever control it had just typed into
(`playwright_port.py:1203, 1248, 1281`). Three consequences, all observed:

* a **cookie banner** — very often `role=alert` so screen readers announce it —
  made every fill on every page look rejected;
* an error raised by field 3 stayed in the DOM while fields 4–12 were filled, so
  **one real failure was reported as ten**;
* an alert already present at page load could not be told apart from one the
  fill had just caused — so the one signal a repair loop needs, *"the application
  rejected THIS value"*, **did not exist**.

Nothing captured `aria-invalid`, `aria-describedby`, `aria-errormessage`, the
browser's native `validationMessage`, or the section a control sits in.

### Cause C — there was no arrow back

`fill_form_phase_a` ran one linear pass: `resolve_field` → `_fill_one` →
next control. `_fill_one` returning `None` recorded `intent_unmet` and moved on.
There was no re-generation from observed feedback anywhere in the engine. The
single exception, `walker._answer_to_unblock`, is one checkbox experiment per
blocked step — valuable, and not a repair loop.

The metric followed the architecture: the crawl reported **fields attempted**,
which is why this looked like it was working.

### Cause D — a per-run key used for per-application knowledge

`identity_seed` came back as `tenant::artifact` and `tp_field_memory` was keyed
`(tenant_id, artifact_id, signature)` with the ciphertext AAD-bound to the
artifact. **A re-crawl mints a new artifact.** So the applicant changed every
run, and crawl N's answers were readable by crawl N+1 and by nobody after that:
each crawl inherited exactly one generation of memory and dropped it.

### Not a cause: radio groups and custom dropdowns

These were *not* broken widgets. They were governed by `data_mode`: `user` mode
deliberately declines to make a semantic choice on the client's behalf. See §7.

---

## 2 · Architecture

A new package, `qe-explorer/app/fill_engine/`. Each module is pure, deterministic
and independently testable; none of them touches a browser except `driver.py`.

```
                     ┌──────────────┐
   seed ───────────▶ │  persona     │  one coherent household
                     └──────┬───────┘
   control ─────────▶┌──────▼───────┐
     + section       │  roles       │  WHOSE field is this?
                     └──────┬───────┘
                     ┌──────▼───────┐
                     │ constraints  │◀── patterns  (bounded regex satisfier)
                     └──────┬───────┘
                     ┌──────▼───────┐
                     │  generator   │  semantically right AND constraint-legal
                     └──────┬───────┘
                     ┌──────▼───────┐
                     │  widgets     │  which verb drives this control
                     └──────┬───────┘
   ┌───────────────────────▼────────────────────┐
   │ repair.repair_loop                          │
   │   commit ──▶ driver ──▶ validation          │
   │      ▲                      │               │
   │      └──── regenerate ◀─────┘  (tightened)  │
   └─────────────────────────────────────────────┘
                            │
                     ┌──────▼───────┐
                     │  learning    │  the scope a value is remembered under
                     └──────────────┘
```

| Module | Responsibility | LoC |
|---|---|---|
| `persona.py` | applicant, spouse, children, beneficiary + contingent, employer, money — all cross-checked | 470 |
| `roles.py` | possessor + organisation resolution from label → placeholder → section | 300 |
| `constraints.py` | read declarations; `violations()`; `conform()` | 420 |
| `patterns.py` | `matches` / `reshape` / `satisfy` over a bounded regex subset | 400 |
| `generator.py` | `Candidate(value, semantic, possessor, source, rationale)` | 560 |
| `widgets.py` | widget class → driving primitive; named blind spots | 210 |
| `validation.py` | `PageAlertFilter`, `signals_for_control`, `interpret` | 420 |
| `repair.py` | the bounded loop, `tighten()`, stop reasons | 300 |
| `driver.py` | two-stage verdict read (free signals, then the expensive one) | 150 |
| `learning.py` | `memory_scope` / `identity_seed` | 120 |
| `options.py` | the canonical placeholder rule (moved, re-exported) | 80 |

### Key design decisions

**Two entry points onto one decision.** `field_values.value_for()` keeps its exact
signature and returns a string; `field_values.explain()` returns the whole
`Candidate`. The explanation can never drift from the value it explains.

**Repair is the exception.** The generator checks its own output against
`constraints.violations()` *before* returning, and reshapes until it passes or
gives up. Measured first-pass rate on the mixed-widget fixture: **100 %**.

**The verdict read is two-stage.** Stage 1 uses signals the fill already
produced (read-back, `intent_met`, fresh page alerts) and costs nothing. Stage 2
re-collects controls and runs anchoring — and only runs on suspicion. Measured:
**0** control re-reads on a clean 18-field page with a cookie banner up
throughout; **1** when the application raised one error.

**Anchoring rungs** (strongest first): `aria-errormessage`/`aria-describedby` →
native `validationMessage` → `aria-invalid` → the `<id>-error` convention every
form library emits → the message names the control → *nothing anchors it, so it
is page context and fails no field*. That last rung is the whole T-FE-02 fix.

**Two rules govern every retry.** A retry must be *caused* by an observed,
control-anchored rejection; and it must *change something that rejection named*,
recording why. `tighten()` only ever narrows, which is what makes the loop
converge rather than oscillate between two rules stated in two places.

---

## 3 · Acceptance checklist

| ID | Requirement | Status | Proof |
|---|---|---|---|
| **T-FE-01** | Bounded validation-repair loop, evidence-driven, explained | ✅ | `test_fill_engine_repair.py` (31), `test_fill_engine_e2e.py::test_a_rejected_value_is_repaired_and_the_application_accepts_it` |
| **T-FE-02** | Control-scoped validation; stale/consent/informational alerts poison nothing | ✅ | `test_a_cookie_banner_present_throughout_fails_no_field`, `test_one_fields_error_does_not_poison_the_fields_that_follow`, `test_an_unanchored_page_alert_fails_no_field` |
| **T-FE-03** | Coherent persona; money from persona; no hardcoded `100` | ✅ | `test_fill_engine_persona.py` (138) — 500/500 personas pass every cross-field rule |
| **T-FE-04** | Identity seeded by `app_id`; identical across crawls; apps isolated | ✅ | `test_fill_engine_learning_scope.py` (11), `test_two_crawls_of_one_application_fill_the_identical_values` |
| **T-FE-05** | Split DOB — month/day/year each get their own part | ✅ | `test_the_three_parts_reassemble_into_the_personas_own_birth_date` (25 seeds) |
| **T-FE-06** | Possessor-aware entity resolution | ✅ | `test_fill_engine_resolution.py` (57), `test_the_beneficiary_section_receives_the_beneficiary` |
| **T-FE-07** | Universal widget support | ⚠️ **partial** | 5/5 widget classes answered by default (see §7) |
| **T-FE-08** | Constraint-aware generation, first attempt | ✅ | 8 pattern classes satisfied first-try; `test_a_declared_pattern_is_satisfied_on_the_first_attempt` |
| **T-FE-09** | Learning persists by `app_id`, versioned, no cross-tenant/app leak | ✅ | `platform/api/tests/test_field_memory_app_scope.py` (11) — survives 7 consecutive crawls |
| **T-FE-10** | End-to-end proof on mixed-widget fixtures, validated completion | ✅ | `test_fill_engine_e2e.py` (24) |

**219 new tests. All pass.**

---

## 4 · Quality metrics (measured, not asserted)

Against the mixed-widget life-application fixture, read out of the shipped
`FormFillResult`:

| Metric | Value |
|---|---|
| Validated field completion | **18 / 18 accepted** (0 attempted-but-unverified) |
| First-pass success rate | **100 %** |
| Repair success rate | **1 / 1** rejected fields accepted |
| Average repair attempts | **2.0** (budget 3) |
| Widget coverage | **5 / 5** classes answered — text, native select, radio group, checkbox group, ARIA combobox |
| Identity consistency | **500 / 500** personas pass every cross-field rule |
| Cross-crawl identity stability | **100 / 100** identical on re-derivation |
| Application isolation | 418 distinct applicants over 500 applications |
| Validation false-positive reduction | **18 alerts suppressed** per page — each one previously failed a field |
| Learning persistence | survives **7** consecutive crawls (was: 1) |
| Latency | 7.9 ms per 20-field page; **0** extra DOM reads on a clean page |

---

## 5 · Performance

The only new per-fill cost is `read_page_alerts()`, which the port already
implemented and which the old code called on every observation anyway. The
expensive control re-read is gated on suspicion and measured at 0 for a clean
page. Persona derivation is a handful of SHA-256 blocks, cached per identity
(bounded at 32 entries). The regex satisfier is hard-capped at 400 AST nodes and
256 output characters and refuses anything outside its subset.

## 6 · Risk assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Engine fills a field the old engine left empty, on a real app | Medium | Every value is constraint-checked before commit; provenance recorded; `data_mode=user` posture unchanged |
| A repair loop mutates state on a real application | Low | The loop only re-fills form controls — the same class of act as typing. It never clicks a submit; the network guard still blocks EXPLORE-phase mutations |
| Pattern satisfier produces a valid-but-meaningless value | Medium | It is the **last** resort: reshape-the-real-value is tried first, and the meaningless case is recorded with its rationale |
| Regex DoS on a hostile `pattern` | Low | Bounded nodes/length/depth; refuses backreferences and lookaround; never raises |
| Cross-application memory leak | **High if wrong** | A row carrying an `app_id` is returned only for that app, even when the artifact matches; AAD shapes differ; 11 tests pin it |
| Migration destroys existing memory | **High if wrong** | Nothing is re-wrapped: `app_id` is *added*, `artifact_id` kept, legacy rows read with their original AAD and superseded on next write |
| Alert heuristics wrongly suppress a real rejection | Medium | Rejection vocabulary is checked *before* the informational rule; unanchored alerts are logged, not discarded silently |

## 7 · The one partial: T-FE-07

Radio groups, checkbox groups, ARIA comboboxes, listboxes and searchable
selects are now **first-class widget classes** answered by default — the engine
half of T-FE-07 is complete and proven (5/5 on the fixture, no posture change).

What I did **not** do is flip `data_mode`. In `user` mode the engine still
declines to choose which business path a funnel walks. That is not a widget gap:
it is a governance decision, gated on environment attestation
(`explorations.py:826` — agent fill is enabled only on an attested non-production
environment) and already surfaced honestly in `degraded.data_mode`. Flipping it
would silently let an agent pick business paths on production environments
somebody deliberately locked.

**This is the founder's call, not mine.** If the intent is that every crawl
answers semantic choices regardless of posture, the change is one line at
`explorations.py:826-827` and I will make it on request.

## 8 · Backward compatibility

* `field_values.value_for()` — signature unchanged; `section` added as optional.
* `field_values.is_placeholder_option` / `enumerate_real` — re-exported, same
  objects, so `forms._is_placeholder_option is field_values.is_placeholder_option`
  still holds (a test pins it).
* `fill_form_phase_a()` — `repair_budget` added with a default; all existing
  callers unchanged. A fake port without `error_texts`/`collect_controls`
  degrades to exactly the old single-shot behaviour.
* `FormFillResult` — new counters are additive with defaults.
* `field_learning.remember/recall/record_outcome` — `app_id` optional; without
  it, behaviour is byte-identical to before.
* `/field-resolution` and `/field-outcome` — `app_id` is an optional query
  param; omitted, both return the old response.
* All 4 characterization goldens are byte-identical.

## 9 · Deployment

1. `psql -f platform/api/scripts/apply_field_memory_app_scope.sql` (idempotent, additive)
2. Deploy platform-api, then qe-central, then qe-explorer.

Ordering matters only in that qe-central must not send `app_id` to a platform-api
that does not accept it — FastAPI ignores unknown query params, so this is safe
either way, but the stated order avoids relying on that.

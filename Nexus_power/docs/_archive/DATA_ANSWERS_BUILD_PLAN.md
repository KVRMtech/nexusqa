# DATA & ANSWERS — Production Build Plan (evidence-based, phase-by-phase)

**Scope:** everything required to make the onboarding **Data** tab (seed test
inputs) and **Answers** tab (the ground-truth oracle) *actually work end-to-end*
and become the differentiator — from their real current state to a production-
ready, 1000+‑clients/day capability.

**This is an audited, evidence-based plan (2026-07-12).** It was written AFTER
tracing the real data flow, because an earlier walkthrough described these tabs as
working when the code shows they do not. Every status below is verified against
code (file:line).

**Status legend:** `[✅ verified]` `[⚠️ partial]` `[❌ missing/broken]` `[🔒 gate]`

---

## GROUND TRUTH — what the audit found

**Blunt summary:** both tabs are **collected in the UI and then ignored.** The Data
tab is a *silent no-op* (a schema mismatch drops the values); the Answers tab's
headline capability (*prove the app produced the correct business value*) **does
not exist.** The underlying engines are good — they're just not fed/built.

| Component | Status | Evidence |
|---|---|---|
| Form-filler **engine** (resolve field→value: exact/semantic/regex) | `[✅ strong]` | `engines/qe-explorer/app/forms.py:58-105 AnswerKey`, `fill_form_phase_a` |
| **Wizard → engine wiring** | `[❌ BROKEN]` | Wizard emits `answer_key:{notes,answers}` (`onboarding/index.tsx:181`); engine reads `{exact,semantic,regex_rules}` (`forms.py:77-79`) → `from_payload` returns **empty** |
| Fill behaviour on empty key | `[❌ silent no-op]` | `forms.py:183` `if value is None: continue` — **skips, no heuristic fallback** |
| Free-text **"Seed data notes"** (the Data field) | `[❌ unused]` | no code consumes `notes`; engine takes only structured keys |
| qe-central passes answer_key verbatim | `[✅]` | `explorations.py:443,483` `dict(row.answer_key)`, no transform to `{exact,semantic}` |
| **Business-value oracle** (verify output == expected) | `[❌ does not exist]` | `answer_key` appears **nowhere** in `platform/api` (the factory that runs+verifies); grep = 0 |
| Behaviour oracle (PROVEN vs INFERRED; nav/confirm/health/semantic) | `[✅ real, grounded]` | `platform/api/app/services/oracle_scorecard.py`, `test_factory/network_oracle.py`, `semantic_oracle.py` |
| Regression assertions (behaviour vs CAPTURED baseline) | `[✅]` | generated Playwright asserts grounded crawl behaviour (toHaveURL/`__nxTok`), NOT client values |
| `answer_key` in coverage/governance accounting | `[⚠️ tracked, not evaluated]` | `qe-central/services/coverage.py:63 SOURCE_ANSWER_KEY`, `gov_models.py:162` provenance, `touch_meter` edit audit |
| The only `expected` column | `[n/a]` | `qe-central/db/models.py:140` = internal REFUSE/green-wash self-test (`qe_harness_runs`), not a client oracle |

**The crucial distinction (state it to every buyer honestly):**
- **What Verdict does today:** proves **REGRESSION** — the app still *behaves* as it
  did at baseline (grounded, honest, ahead of competitors on behaviour).
- **What Verdict does NOT do today:** prove **CORRECTNESS** — that a computed value
  (e.g. a `$28.40` premium) is *right* per your business rules. The Answers tab was
  built to do this; it isn't wired to anything.

---

## PHASE 0 — Fix the Data integration (the silent-drop bug) 🔒

**Goal:** the Data tab actually fills forms. This is a small, high-value fix that
unblocks every real form-gated flow (quotes, applications, checkout).

### 0.1 Map wizard fields → the engine's contract `[❌ broken]`
- The engine wants `answer_key = {exact:{name→value}, semantic:{keyword→value},
  regex_rules:[{pattern,value}]}`. The wizard sends `{notes, answers}`.
- Fix (choose one, documented):
  - (a) **Wizard emits the right shape** — the Data tab becomes a small key/value
    grid (field-name → value) that maps to `exact`/`semantic`; the raw JSON textarea
    accepts `{exact,semantic,regex_rules}` directly; OR
  - (b) **qe-central transforms** the stored `answer_key` into `{exact,semantic,
    regex_rules}` at dispatch (`explorations.py`) — accept both shapes for back-compat.
- Recommendation: **(a) for structured input + (b) as a tolerant adapter** so old
  rows and hand-authored JSON both work.
- **Files:** `verdict-portal/src/features/onboarding/index.tsx`, `explorations.py`
  (adapter), `forms.py` (unchanged — already correct).
- **Accept:** onboard an app with seed values → crawl a real form → the fields are
  **filled** (`qec.forms.phase_a filled=N`, N>0) → the crawl proceeds PAST the form.
- **Effort:** S (hours). **Priority: P0 — it's a bug.**

### 0.2 Prove it live `[❌]`
- On the acme-life quote form (age/coverage/term/state/smoker), verify the crawler
  fills all fields from the answer key and reaches Apply→Review→Bind.
- **Accept:** crawl visits Apply/Review/Bind (today it stalls at the quote form).

**Phase 0 exit gate:** a real, validation-gated form is filled from seed data and the
crawl reaches the flows behind it.

---

## PHASE 1 — The Business-Value Oracle (the moat) 🔒

**Goal:** consume the answer key you already collect and **prove the app's output is
correct**, grounded. This is the single biggest differentiator and it does not
exist yet.

### 1.1 Plumb `answer_key` into the factory `[❌ absent]`
- `platform/api` never receives the answer key. Pass it from qe-central →
  platform-api on generate/run (the factory builds + runs the tests).
- **Files:** `qe-central/clients/factory.py` (send answer_key), `platform/api`
  test-factory generate/run endpoints (accept it, thread into generation).
- **Effort:** M.

### 1.2 Grounded value assertions `[❌]`
- Define an **expected-outcome** section in the answer key, distinct from fill data:
  `{ outcomes: [{ when:{persona}, field:"monthly_premium", equals:28.40,
  tolerance:0.50, source_hint:"#premium" }] }`.
- At generation, emit a REAL Playwright assertion that captures the observed value
  from a grounded DOM node (provenance recorded) and compares to `expected`.
- **PROVEN only when grounded:** if the observed value can't be captured from a
  real node, the assertion is `INFERRED`/`unverifiable` — never a silent pass
  (reuse the `oracle_scorecard` PROVEN-vs-INFERRED doctrine + verbatim grounding).
- Tie every verdict to the signed ledger: *"$500k/20yr/35/non-smoker → $28.40
  ±0.50, PROVEN from `#premium`, commit abc123, video attached."*
- **Files:** `platform/api/services/script_factory/compiler.py` (assertion emit),
  a new `services/test_factory/value_oracle.py`.
- **Accept:** a correct app PROVES the value; a seeded `$2.84` bug FAILS with
  before/after + video; an ungrounded value is `unverifiable`, never green.
- **Effort:** L (the differentiator; grounded, deterministic at runtime).

**Phase 1 exit gate:** a business value is PROVEN-correct against the answer key on a
proving-ground app; a wrong value is caught with evidence.

---

## PHASE 2 — LLM "brief → contract" compiler (both tabs, at scale)

**Goal:** let clients write plain English (what they already type today) and compile
it — verbatim-grounded — into the structured seed + expected-outcome contracts, so
onboarding is tractable for 1000+ clients.

### 2.1 Notes → structured seed `[❌]`
- Compile the free-text "Seed data notes" ("age 35, non-smoker, $500k, TX") into
  `{exact,semantic,regex_rules}` at **authoring time** (never at runtime).
- **Grounded:** the compiler proposes; a value it can't tie to a real field/label
  is flagged for human confirmation, never silently used.
- **Effort:** M.

### 2.2 Plain-English answers → expected-outcome oracles `[❌]`
- Compile "a 35-yo non-smoker quotes about $28/mo; min age 18; decline codes
  UW-17/22" into the `outcomes` + rule contracts of Phase 1/3.
- **Effort:** M.

**Phase 2 exit gate:** a plain-English brief produces a working, reviewable seed +
oracle contract; the operator confirms before it's active.

---

## PHASE 3 — Rule oracles (invariants, not just point values) `[❌]`

**Goal:** assert business INVARIANTS that catch whole classes of regressions — what
an actuary certifies.

- Examples: *smoker premium ≥ 40% higher than non-smoker*; *coverage ≤ $2M always*;
  *age < 18 always declined*; *quote monotonic in coverage*.
- Emitted as grounded, deterministic assertions over captured values across
  personas; a violation is a FAIL with the exact inputs.
- **Files:** `value_oracle.py` (rule evaluator), answer-key schema (`rules:[...]`).
- **Effort:** M–L. **Depends on:** P1 (value capture) + P2 (authoring).

---

## PHASE 4 — Scale, versioning & governance (1000+/day)

- **Deterministic runtime:** value/rule evaluation is pure comparison over captured
  DOM values — **no per-assertion LLM at run time** (LLM only at authoring). Keeps
  cost flat at scale.
- **Answer-key versioning + approval:** edits are versioned + e-signed (partially
  present: `touch_meter.answer_key_edit`, `gov_models` provenance) — extend to a
  full review/approval + rollback.
- **Coverage integration:** `coverage.py` already knows `SOURCE_ANSWER_KEY`; surface
  "% of business outcomes with a grounded oracle" as a coverage metric.
- **PII:** seed data + expected values are client PII — encrypt at rest (answer_key
  is stored plaintext JSONB today, like repo_binding) and scrub logs.
- **Effort:** M.

**Phase 4 exit gate:** answer keys are versioned, approved, encrypted, and evaluated
deterministically at 1000+/day.

---

## Competitive positioning (honest)
- **Behaviour oracle (A2):** genuinely ahead — PROVEN-vs-INFERRED grounding beats
  "no error → pass."
- **Value assertion (A1):** currently BEHIND the pitch — testRigor/others let users
  write plain-English value checks; we collect the key but don't evaluate it. P1
  closes this AND leapfrogs, because our version is **grounded + signed** (they
  aren't).
- **Filler engine (D1):** exact/semantic/regex resolution is more capable than naive
  fillers — once fed (P0).

## Dependency chain
```
P0 fix Data mismatch (unblocks form-gated flows)
   └─> P1 value oracle (plumb answer_key → factory → grounded assertion)
          └─> P3 rule oracles (invariants over captured values)
P2 LLM brief→contract  (feeds P0 seed + P1/P3 oracles; authoring-time)
P4 scale/versioning/encryption  (wraps all)
```

## Effort reality
- **P0 is small** (hours) — a schema-adapter bug fix + a live proof. Do it first.
- **P1 is the big, valuable build** (the grounded value oracle) — reuses the
  PROVEN-vs-INFERRED doctrine + the generator, but `answer_key` must first reach the
  factory (it doesn't today).
- **P2/P3** turn it into a plain-English, invariant-checking product. **P4** makes it
  safe + cheap at scale.

## Cross-cutting
- **Never green-wash:** an ungrounded value/rule is `unverifiable`, never a pass.
- **Grounded + provenance:** every asserted value carries `file/DOM-node` provenance
  and ties to the signed verdict ledger.
- **Deterministic at runtime; LLM only at authoring.**
- **RLS + encryption** for all client seed/answer data.

## Definition of done
A client provides seed data + expected answers (in plain English, compiled to a
grounded contract), the crawler **fills real forms** with the seed data and reaches
the deep flows, and every business-critical output is **PROVEN correct** (or honestly
`unverifiable`) against the answer key — with provenance, video, and a signed ledger
entry — deterministically, at 1000+/day, with versioned/approved/encrypted keys.

# Record once, run anywhere — final implementation plan

**Status:** approved, not started · **Date:** 2026-07-28
**Branch:** `feat/record-once-run-anywhere` · **Restore point:** tag `pre-member-data-resolver-20260728` (`ff77c40`)
**Companion docs:** [RECORD_ONCE_RUN_ANYWHERE_PLAN.md](RECORD_ONCE_RUN_ANYWHERE_PLAN.md) (the login-recording design this continues),
[PERSONA_ENVIRONMENT_MATRIX_PLAN.md](PERSONA_ENVIRONMENT_MATRIX_PLAN.md) (RUN = Suite × Environment × Member)

---

## 0. Recovery — taken before any implementation

| Artifact | Size | Verification |
|---|---|---|
| `C:\Users\srika\nexus_backups\nexusqa-allrefs-20260728.bundle` | 273 MB | All branches + tags. **Restore-tested**: cloned back, HEAD `14f81f9`, 14 branches, tags intact |
| `C:\Users\srika\nexus_backups\nexusqa-source-20260728.zip` | 32 MB | Working tree, 2,129 files, key sources confirmed present |
| git tag `pre-member-data-resolver-20260728` | — | → `ff77c40` |

Restore the whole repository with:

```
git clone --branch feat/record-once-run-anywhere \
  C:\Users\srika\nexus_backups\nexusqa-allrefs-20260728.bundle <dest>
```

---

## 1. The problem, precisely

Logging in as any member in any environment **already works**. What does not work is what the test then *asserts*.

The generator bakes the crawl member's literal values into every step
(`platform/api/app/services/test_factory/generator.py:1176-1184`):

```python
action   = f"Enter '{value}' in the '{label}' field"
expected = f"'{label}' shows '{value}'"
data_ref = value
```

`factory_test_cases` has **no member column** — a case does not record which member produced it.

**Consequence.** Run that suite as a different member and the script logs in correctly as Member B, then types
Member A's values into B's session and asserts against what it just typed → **green, and wrong**. A mismatch on
displayed member data is a non-failing soft warning (`script_factory/compiler.py:566`).

**Why.** `tp_persona_expected_values`, its endpoints and the diff classifier all exist, but **nothing reads them at
run time** — every caller is reporting:

```
routers/test_factory.py:8008          GET endpoint (echo)
routers/test_factory.py:8419          /oracle-split (report)
services/test_factory/evidence_report.py:828   Trust Block (report)
```

`persona_store.build_persona_bundle` passes **login credentials only**. `compiler.compile_case()` takes no persona
parameter — the compiler is persona-blind by construction.

Cardinality is the same story: counts are stored, planned and injected as `NEXUS_REPETITION`, but `__nxRepeat()`
lives in the globalSetup template (`compiler.py:2111`) and **is never called by any spec**.

---

## 2. Rules this plan holds itself to

1. **Earned, never guessed.** Whether a value is member-specific is decided by *observing the same flow as two
   members and comparing*. Never by matching a field name against a list. There is no vocabulary anywhere.
2. **No defaults for identity.** No field, slot or environment key gets a built-in default. Not supplied and not
   observed means *missing* — and missing is surfaced, not filled in.
3. **Missing means blocked.** A member-specific value with no answer for the running member blocks the run. Never
   substituted with another member's value, never quietly skipped.
4. **Off until proven.** Each behavioural phase ships behind its own flag, default off. With the flag off the
   compiled output is byte-identical to today.

---

## 3. What is already true

| Capability | State |
|---|---|
| Recipe / environment / member card held separately, composed at run time | Live |
| Record a login by performing it — identifiers only, no credential values | Live, proven on the real app |
| Log in as any member, in any environment | Live |
| Environment swap (same login, different box) | Live |
| Reuse fingerprint + match decision | Built, not yet surfaced |
| **Test cases asserting the running member's data** | **Not built — this plan** |

---

## Track A · correctness

### Phase 0 — Safety net  *(mostly done)*

Restore point taken (§0). **Remaining:** characterisation tests capturing what a compiled script emits *today*, so
phases 2–5 prove they changed only what they intended.

*Done when:* a deliberate change to compiler output fails a test that names it.

### Phase 1 — Learn which values belong to a person  *(foundation)*

Everything downstream needs to know which values are member-specific. Decided by evidence; changes no run behaviour.

- Harvest the values a run actually observed, per member (the inventory already exists as
  `_suite_observed_values`, `routers/test_factory.py:8325-8340`).
- Compare two members: differs ⇒ member-specific; identical ⇒ shared; unstable across repeats ⇒ volatile.
- Persist each verdict with its evidence (`tp_value_classifications`, `tp_persona_expected_values`).

*Verified by:* two members over one suite yield a verdict for every observed value; a value seen for only one
member is **unknown**, never assumed shared.
*Nothing breaks:* write-only, nothing reads it yet.

### Phase 2 — Resolve values for the running member  *(core fix)*

The substitution already exists in the compiled script — it is fed from the request body today.

```
compiled today:   (D['<field>'] ?? '<recorded literal>')     compiler.py:97-107
D today:          body.data / body.data_by_test              routers/test_factory.py:1013-1023
D after phase 2:  the running member's answers, resolved at dispatch
```

- Load the running member's answers at dispatch; merge into the override map.
- Explicit caller-supplied data still wins, so existing runs are unaffected.

*Verified by:* same suite, two members ⇒ each drives its own values; flag off ⇒ compiled output byte-identical.
**Ships together with Phase 3. Never on its own.**

### Phase 3 — Block when the answer is missing  *(honesty gate)*

Phase 2 alone falls back to the recorded literal when a member has no answer — the original bug wearing a
different hat.

- Before dispatch, list every member-specific value the selected cases assert.
- Any without an answer for this member ⇒ **BLOCKED**, naming which and for whom.
- Reuses the existing block taxonomy (`routers/test_factory.py:3922-4086`) — same shape as a missing credential card.

*Done when:* a member with one missing answer is blocked and told exactly which.

### Phase 4 — Separate what you type from what you assert  *(core fix)*

A member-specific value typed into a field is **input**; displayed on a page it is an **expectation**. Today both
are the same baked literal.

- Member-specific inputs resolved from the member before the step runs.
- Member-specific assertions checked against that member's answers.
- Shared values keep the recorded literal, untouched.

*Verified by:* a pre-populated field asserts the running member's value; a shared value still fails red if the
application changes it.

### Phase 5 — Counts that vary by member

Members differ in *how many* of a thing they have. Storage, plan and env injection exist
(`persona_store.py:711-751`, `persona_governance.py:177-189`, `routers/test_factory.py:4096-4105,4146`);
the consuming helper is never called.

- Generator marks a repeated block as repeatable rather than emitting a flat list.
- Compiler drives that block from the injected per-member count.

*Verified by:* one suite runs the block the right number of times for two differently-shaped members; an unknown
count runs once and says so.

---

## Track B · login coverage

### Phase 6 — Close the known login gaps

The credential model is generic — arbitrary field names, any count, any number of steps (proven live: two
different slot sets produce two different reuse keys through one engine). Two mechanisms are currently
mis-recorded:

- **Iframe-hosted login** (embedded identity widgets) — skews the recorded login path / domain.
- **SSO redirect** to an identity provider — keys on the provider's domain and emits a host-less `goto` replayed
  against the wrong host.

An audit is enumerating the rest across five areas: credential shapes, second factors, SSO/embedding, front-end
technology (controlled inputs, shadow DOM, no-`<form>` logins, autofill, on-screen keypads) and non-form auth
(basic auth, client certificates, token headers, session import). Its **works / degrades / blocked** matrix
appends here when complete.

**Permanently out of scope — for every vendor, not just this product.** A code sent by SMS or email, a CAPTCHA,
or a passkey/biometric cannot be recorded by anyone. For those we connect to a session instead of recording one
(login hook / session import — both already exist). Stated plainly so it is never discovered in a customer POC.

*Done when:* the matrix is published and every "degrades" has a named workaround.

---

## Track C · workflow

### Phase 7 — Record at the start, not the end

Recording belongs at the bottom of the onboarding **Access** page, not in a screen only reachable after a crawl.

- Record control at the foot of Access — optional, clearly labelled.
- One pass captures the environment (landed URL + routing cookies) **and** the login.
- Held against the wizard draft; written only when the app is created.

*Verified by:* existing Access fields still work typed by hand; abandoning the wizard writes nothing; a public
flow with no login records the environment alone.

### Phase 8 — Don't make the next tester record

The matching logic is built and deployed (`login_fingerprint.propose_reuse`,
`persona_store.find_recipes_by_login_type`) — it is simply never shown to anyone.

- On onboarding, fingerprint the login and check the tenant's library.
- Match ⇒ offer reuse; several ⇒ ask which; none ⇒ record.

*Verified by:* a second app on the same host is offered the existing login; two genuinely different logins on one
host are never conflated.

> The fingerprint uses the **registrable domain**, so `uat.<host>`, `prod.<host>` and a numbered box all reduce to
> the same key. Different environments do **not** fragment the recipe library.

---

## Track D · proof

### Phase 9 — Prove it on the real application

| Case | Required outcome |
|---|---|
| Member A, environment 1 (as recorded) | green, values proven |
| Member B, environment 1 | green on B's own values |
| Member A, environment 2 | green, same values |
| Member B, environment 2 | green on B's own values |
| Member C, no answers on file | **BLOCKED**, naming what is missing |
| A deliberately wrong answer | **RED** |

**The last two rows decide it.** A suite that only ever goes green proves nothing. Until a missing answer blocks
and a wrong answer goes red, the assertions are decorative.

---

## 4. Sequencing and rollback

| Phase | Changes run behaviour? | Reverted by |
|---|---|---|
| 0 · Safety net | No | — |
| 1 · Classification | No — write only | ignore the data |
| 2 · Resolver | Yes, flagged | flag off |
| 3 · Block on missing | Yes, flagged | flag off |
| 4 · Input vs assertion | Yes, flagged | flag off |
| 5 · Counts | Yes, flagged | flag off |
| 6 · Login coverage | No — recorder only | revert recorder file |
| 7 · Recorder placement | No — additive UI | hide the control |
| 8 · Reuse prompt | No — a suggestion | hide the prompt |

**The order is not arbitrary.** Phase 1 must precede 2 — resolving a value requires first knowing it belongs to a
person. Phase 3 must ship *with* 2, never after. Tracks B and C can run in parallel with A; only Track A gates
Phase 9.

---

## 5. What this deliberately does not attempt

- **Inventing answers.** If nobody has observed what a member should see, the system says so and blocks.
- **Guessing from names.** No field is treated as identity because of what it is called.
- **Rewriting the generator.** Recorded literals remain the fallback for shared values; only member-specific ones
  are resolved.
- **Touching the frozen pipeline.** Every change is additive to the persona, dispatch and recorder layers.

---

**The one-line test of success:** run the same suite as a member it has never seen, and it either proves that
member's data or refuses — but never reports green on someone else's.

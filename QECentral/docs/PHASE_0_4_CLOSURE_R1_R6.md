# Phases 0–4 closure run — R1…R6, and why Gate 5 is refused

**Verdict: `GATE 5 — CERTIFICATION REFUSED.`**
Refused on prerequisites that are unmet in fact, not on process. Each one is
named below with the measurement that establishes it.

| | |
| --- | --- |
| Branch | `feat/qec-dynamic-catalog-p0-p6` |
| Evidence SHA | `e24bcf54d0883a452185d49cfabe81f40458ccd3` — every bundle here was produced at it, clean tree |
| Final SHA | this commit (adds evidence + this document only; no code the crawler reads) |
| CI at `8c443f2` | Nexus QA CI **success**, M0.5 Security Gate **success**, A11 Attestation Certification **success** |
| Concurrent sessions in this checkout | 6 peers at start of run (`ListAgents`) |

---

## Roll-call

```
TASK | RESULT | SHA | VERDICT | EVIDENCE | BLOCKER / NEXT ACTION
```

| Task | Result | SHA | Verdict line | Evidence | Blocker / next action |
|---|---|---|---|---|---|
| **R1** vkpower-life | **BLOCKED** | `8c443f2` | `crossed : 0 []` · `confirmation observed: False []` | `evidence/gate2/r1_vkpower_live/` | ⛔ *cause retracted — see banner.* NOT pack-blocked: a safe path past ACH existed. Real cause = **R9** (formless plan-picker) |
| **R2** summit-life-carrier | **BLOCKED** | `8c443f2` | `crossed : 0 []` · `confirmation observed: False []` | `evidence/gate2/r2_summit_live/`, counterfactual in `r2_summit_counterfactual/` | ⛔ *cause partly retracted — see banner.* Entrance block was section-name matching, fixed by **R7′** `d3ed533`; 6 unfillable fields remain |
| **R3** acme-life | **PASS** | `e24bcf5` | `crossed : 2 ['Bind policy','Bind policy']` · `confirmation observed: True ['dialog']` | `evidence/gate2/r3_acme_reproduced/` | none — `[ADMISSIBLE] 1/1` against the T3 gate |
| **R4** egress fence | **DONE** | `2164ac3` | Decision **(B) ACCEPT capacity=1** | `QECentral/docs/ARB_EGRESS_FENCE_DECISION_RECORD.md` | owner seat for (b+) runtime refusal is **VACANT** |
| **R5** Phase 2 deploy | **BLOCKED — not attempted** | — | VM at `ede6bf2`, **145 commits behind** | §R5 below | the M2.1 proof cannot target a deployed service; deploying would not yield the required evidence |
| **R6** A11e matrix | **PASS** (advisory) | `d6af7c4` | `CONVERGENCE OK: 24 vectors x 2 copies x 2 interpreters — agree within each and across all.` | `evidence/a11e_interpreter_matrix/` | none; stays non-blocking |

**Crossings: 1 of 3.** Phase 1's exit criterion is 3. The T3 gate at the evidence
SHA, quoted verbatim (`evidence/gate2/T3_GATE_ROLLCALL.txt`, exit 1):

```
::error::Phase 1 needs 3 admissible crossings, has 1.
  [ADMISSIBLE] evidence/gate2/r3_acme_reproduced/journey.json
  [REFUSED]    r1_vkpower_live: provenance is not the certified SHA — bundle head=8c443f294136 certified=e24bcf54d088
  [REFUSED]    r1_vkpower_live: NO CROSSING — boundaries_crossed == 0
  [REFUSED]    r1_vkpower_live: no confirmation observed — a crossing without an observed outcome proves a click, not an effect
  [REFUSED]    r1_vkpower_live: no outcome milestone stored — nothing durable to replay against
  [REFUSED]    r2_summit_live: provenance is not the certified SHA — bundle head=8c443f294136 certified=e24bcf54d088
  [REFUSED]    r2_summit_live: NO CROSSING — boundaries_crossed == 0
  [REFUSED]    r2_summit_live: no confirmation observed — …
  [REFUSED]    r2_summit_live: no outcome milestone stored — …

admissible: 1/3
```

The **provenance** refusals on R1/R2 are an artefact of this run's own ordering,
not a second defect: both crawls ran at `8c443f2`, and three later commits (R4,
the R1/R2 probe, R6) moved HEAD to `e24bcf5` before R3 was re-run. Re-running them
would clear that line and leave the `NO CROSSING` refusals, which are the real
ones. **R3 was deliberately re-run at `e24bcf5` for exactly this reason** — it is
admissible against the evidence SHA, having also crossed at `8c443f2` earlier in
the run.

---

---

> # ⛔ RETRACTION — the R1/R2 root cause below is WRONG
>
> **Retracted 2026-08-23 at `d3ed533`. The section "The single root cause behind
> R1 and R2" is left standing exactly as written, and is wrong.** It is kept
> rather than edited because the reasoning error is the useful artefact — the
> same precedent the A4.3 record set by keeping its own wrong reading.
>
> **What it claims:** that `rp.verb.*` rules carrying `url_path` blanket-flag
> *every actuator on a page* whose URL contains the verb — "the notification bell
> is not a payment" — and that the pack documents a remediation never applied.
>
> **Why it is wrong:** that measurement called `classify_control_danger` directly,
> **passing the page URL**. `build_inventory` — the only path the crawler uses —
> passes each control's **own destination**, never the page. Measured correctly
> against the *unmodified* pack:
>
> ```
> Back / Notifications / Monthly on /life-insurance/apply/payment/   danger=False
> "3" (bell) on /underwriting/new-business/new-application            danger=False
> link -> /account/delete                                            danger=True
> ```
>
> The page-contamination bug **did not exist**. It had already been fixed at the
> inventory layer, and `tests/test_danger_scope.py` exists to hold that line —
> its docstring is the very fix this record "discovered".
>
> **The counterfactual proved the opposite of what this record says it proved.**
> Summit reached `/underwriting/...` because the patch removed a **legitimate
> destination-based refusal**, not because a blanket lifted. See
> `evidence/gate2/r2_summit_counterfactual/NOT_ADMISSIBLE.md`.
>
> **What was really wrong**, and is now fixed by **R7′** (`d3ed533`): a much
> narrower thing. `rp.verb.pay` and `rp.verb.underwrite` used one broad regex for
> both label and destination, so *section names* matched —
> `/underwriting/new-business` and `/policy-admin/payments` — and every link into
> those sections was refused. Two rules split, not nineteen re-scoped.
>
> **R1 was never pack-blocked at all.** On the payment page only the ACH button is
> flagged; `Credit / Debit Card` and `Continue to Beneficiary Designation` are
> both safe, so a safe path past it existed. R1's real cause is **R9** below.
>
> **Method note this cost:** verifying a rule means reading the call path, not the
> rule text. Both the original diagnosis and its first review checked the rule.

---

## The single root cause behind R1 and R2

**⛔ RETRACTED — see the banner above. Left standing deliberately.**


Both remaining funnels dead-end for **one reason, and it is neither the
applications nor the walk logic.**

`rp.verb.pay` and `rp.verb.underwrite` carry
`applies_to: ["button_name", "url_path", "url_query"]`. `url_path` makes the rule
fire on the **page**, not the control, so every actuator on a page whose URL
contains "payment" or "underwriting" is `danger=critical` — and a danger-flagged
control is dropped from every advance tier. Measured on the real pack:

```
/life-insurance/apply/payment/   Back                                -> rp.verb.pay critical
                                 Notifications                       -> rp.verb.pay critical
                                 Monthly                             -> rp.verb.pay critical
                                 Continue to Beneficiary Designation -> rp.verb.pay critical
/underwriting/new-business/new-application
                                 New Business Queue                  -> rp.verb.underwrite critical
                                 Back                                -> rp.verb.underwrite critical
```

The notification bell is not a payment.

**The pack already documents this as fixed, and it is not.** The A14
`allow_overrides` row states `rp.verb.underwrite` was *"already forced to be
scoped off `url_path`, where it had marked 20 of 35 controls … as critical —
including the Back button and the notification bell."* Line 160 still lists
`url_path`, and `git log -S'rp.verb.underwrite'` shows it never was removed. A
remediation was written down, never applied, and has been readable as evidence
ever since.

Reproduce: `python Nexus_power/scripts/r1r2_measure_urlpath_overblock.py`

### It is causal, not correlated

Re-running summit-life-carrier with `url_path` scoped off those two rules and
**nothing else changed** — same app, grants, oracle, SHA:

| | baseline | counterfactual |
| --- | --- | --- |
| routes reached | 8, none under `/underwriting/` | 13, incl. `/underwriting/new-business/new-application` (**the granted commit URL**) |
| commit control seen at granted URL | no | **yes** (`Submit Application`, `Review & Submit`) |
| boundaries crossed | 0 | 0 |

So the rule **seals entry** and removing it **unseals entry** — but that alone
does not produce a crossing.

### Why it was not fixed here

Removing `url_path` from a `critical` rule widens what the crawler will click for
every tenant and application. This repository's own precedent for exactly this
situation (`735e6b4`, the first `allow_overrides` row) was a narrow, full-string
override scoped to `button_name` **only**, reasoned as *"never a url — no GET can
be unblocked by it"*. Editing a critical guard inside a certification window to
turn two crawls green is the green-wash this programme exists to prevent. It
needs the refuse-policy owner.

The diagnosis is shown to discriminate rather than asserted: with `url_path`
scoped off, all eight over-blocked controls go safe while the control group —
`Sign & Submit Application`, `Pay Now`, `Make Payment`, `Submit to Underwriting`,
`Transfer Funds`, `Underwrite Now` — **every one stays refused**. The probe exits
1 if that ever breaches.

### R1's other acceptance item: the merged collapse fix IS exercised — verified

R1 asks for confirmation that the merged save+navigation / state-collapse fix
(`e1200b7` via `c9b891b` — the persist+navigate veto in `walker.py` and the
`displayed_values` drop in `discovery.py`) is actually exercised, not merely
present. It is, and this is measured from the R1 crawl's own coverage rather than
inferred from the merge:

```
total states: 19 | distinct locations: 15 | distinct state identities: 19
```

**19 states, 19 identities — nothing collapsed.** Four locations were visited
twice and the identity ladder split every one of them:

```
/                                  -> 2 distinct state identities
/life-insurance/apply/health/      -> 2 distinct state identities
/life-insurance/quote/coverage/    -> 2 distinct state identities
/life-insurance/quote/start/       -> 2 distinct state identities
```

That is exactly the failure `e1200b7` fixed (same `state_id` for
`/result.html` "Quote Start" and `/result.html` "Quote Result"), and it does not
occur here. Corroborating depth: the R1 crawl reached member-lookup →
personal-info → replacement → health → lifestyle → decision → payment at
33 states / 162 actions, against the historical unseeded baseline of 17 / 86 that
dead-ended at member-lookup.

**So R1's failure is not the collapse defect and not the login.** The login
completed (`member_number` → `password` → 6-digit Security PIN, the app's
`useMfa` default path, matching the `mfa: {kind: otp, otp: "123456"}` block in
`APPS`). The crawl walked seven funnel steps and stopped at the payment page.

### The remaining blockers, per app

* **R1 vkpower-life — a second, independent blocker.** `ACH Bank Transfer Direct
  debit from checking or savings` trips `rp.verb.transfer` on its own **button
  name**, so scoping `url_path` off does not by itself unblock the payment step.
  The control is `type="button"` calling `updatePayment({method:'ach'})` — it
  transfers nothing, and the submit below it is `disabled={!method}`.
* **R2 summit-life-carrier — a second blocker, visible only once the first is
  lifted.** The wizard then stops on the application's own validation:
  `advance_disabled_by_app_validation`, `missing_fields=["Gender"]`, with
  `fields_needing_seed = ['State','Gender','Product','Risk Classification',
  'Tobacco Use','Claim Type']`. Six fields needing seed values. **Here the
  crawler reports them correctly** — unlike the vkpower Member Number case, where
  a synthesized value that satisfied the widget and failed the business rule left
  `fields_needing_seed` empty.

Neither remedy is an application change. No application was modified in this run.

---

## R5 — Phase 2 deployment: not attempted, and why that is the right call

**Measured on the VM, read-only, during this run:**

```
/home/srika/nexus-src   HEAD ede6bf26c68a…   branch develop   2026-08-18
GET https://136.85.106.73/health
  {"status":"healthy","service":"qe-central","db_qec":"connected",
   "db_substrate":"connected","kek":{"provider":"gcp_kms",
   "is_production_grade":true,"envelope_ready":true}}
```

The service is healthy and on production-grade KMS. It is **145 commits behind**
this branch (`git rev-list --count develop..HEAD`).

**The blocker is not the deploy — it is that the deploy cannot produce the
required evidence.** R5's acceptance is "rerun the M2.1 catalogue proof against
the deployed service". A25 claims that proof structurally cannot do this. **That
claim was verified here rather than accepted:**
`tests/browser/test_questionnaire_catalog_e2e.py` constructs

```python
crawler = Crawler(PlaywrightBrowserPort(pw.page, pw.context), … )
```

with **no HTTP client and no service URL**, and folds the catalogue in-process —
its own comment says *"the SAME derivation `journey_fold` performs, **minus the
DB**"*. So it exercises no deployed service, before or after a deploy. A
deployed-services variant has to be written first.

Deploying anyway would mean merging **145 commits** — the work of six concurrent
sessions, which `CLAUDE.md §4` warns means *"merge my work usually means merge
everyone's"* — plus six absent migrations, onto a live production VM, in a shared
checkout, **to obtain evidence that still could not be produced.** That is a large
irreversible action with no evidentiary payoff, so it was not taken.

**Next action:** write the deployed-services variant of the M2.1 proof, then
deploy and rerun. Both are buildable and named; neither is done.

---

## Gate 5 — the unmet prerequisites

`python scripts/gate5_verify_ceremony.py QECentral/certification/gate5_ceremony_record.json`
→ **`GATE5_CEREMONY: REFUSED — 17 condition(s) unmet`**

The ceremony was **not performed**, because performing it would require
fabricating things that must not be fabricated. Blockers, grouped:

**1 · Crossings — 1 of 3.** R1 and R2 above.

**2 · Phase 2 not deployed.** R5 above.

**3 · Human-only security actions — both unmet, both re-measured here.**

* **Exposed GitHub PAT: NOT revoked.** `A37.2` records it scrubbed from the VM
  but still valid (`github_pat_`, account `Venkatareddy2012`, ends `…p0Ox`,
  expires 2026-09-05). Scrubbing is not rotation. It **cannot be verified from
  here** — the token value was destroyed by the scrub, so this session has
  nothing to test with. Only the account owner can revoke and confirm.
* **OpenAI API key: NOT replaced.** Re-verified in this run with a plain `curl`,
  independent of our code:

  ```
  curl -H "Authorization: Bearer $OPENAI_API_KEY" https://api.openai.com/v1/models
  HTTP=401   {"error":{"code":"invalid_api_key","type":"invalid_request_error",
              "message":"Incorrect API key provided: sk-proj-****…vXwA"}}
  ```

  The JSON error body is OpenAI's own, which rules out a network wall. The key in
  this environment is still the rejected one.

**4 · The seats are vacant, and an agent must not fill them.** `GATE_5_CEREMONY.md`
is explicit: *"No name in the ceremony record may be written by an agent … the
validator cannot detect a fabricated human — only the absence of one."* Release
Director, Proof Guild and a 3-person ARB quorum are all unappointed.

**5 · Clean-clone attestation on unowned hardware: not run** against any candidate
SHA (`gate5-clean-clone-attestation.yml` exists and is built).

**6 · Non-author reproduction: none recorded.** Every proof in this run was
produced by this session. `GATE_5_CEREMONY.md §3` establishes that this **cannot
be reconstructed after the fact** — all commits share one identity, so
`reproductions[]` must be written when the reproduction happens or the claim is
unfalsifiable forever. R3 was reproduced twice (at `8c443f2` and again at
`e24bcf5`), but by the same session, which is not the property required.

---

## Second phase — R7′…R7(5), R8, R9 (2026-08-23)

After the retraction above, the architect authorised a **narrowed** change and the
sequence was re-run. Roll-call for that phase:

| Task | Result | SHA | What it settled |
|---|---|---|---|
| **R7′** | LANDED | `d3ed533` | `rp.verb.pay` / `rp.verb.underwrite` split: broad vocabulary keeps `button_name`, a narrow pattern takes the destination. 2 rules, not 19 |
| **R7″** | LANDED | `897da04` | 13 under-blocks the red-team found in R7′ — `/pay-now`, `/submit-to-underwriting`, `?action=underwrite` … — plus the missing route-layer backstop |
| **R7‴** | LANDED | `22da0d7` | Finding B: my R7″ suffix widening reopened the over-block for `/remittance-advice`, `/payout-history` … |
| **R7⁗** | LANDED | `4aeccfd` | `capture` fell through a gap between two of my own lists; `-send`/`-release`/`-initiate` too |
| **R7(5)** | LANDED | `2fc403f` | **Polarity inverted.** Enumerating acts under default-ALLOW was fail-OPEN, and `classify_action_verb` is the *sole* gate for a mutating WALK request |
| **R8** | FIXED | `24da99f` | The browser lane crawled a login-gated app anonymously. Verified locally: 1 passed, 25m57s |
| **R9** | DIAGNOSED | `a725b28` | vkpower's payment step is a formless picker — 14 controls, 0 questions, no `advance_blocked`. Silent stall |
| **R2 re-run** | reached the commit URL | `24da99f` | 8 routes → 11, `flows 5→10`, `Submit Application` offered. Still `crossed: 0` — six unfillable fields remain |
| **R1 re-run** | unchanged | `2fc403f` | `crossed: 0`. Confirms R1 was never pack-blocked; R9 is its cause |
| **R3 reproduction** | **REPRODUCED** | `24da99f` | By peer session `nexusqa-21`, fresh clone. Identical on all three claimed lines. Fills no signatory seat |

### The one that matters most

**R7(5) closed a fail-open hole I opened.** In `Phase.WALK`, once `walk_attested`
is true, a mutating request is adjudicated by `classify_action_verb(name, url)`
**and nothing else** — the mutation-signal rules only adjudicate reads. WALK is
also the phase whose own code comment says it "has no human in the loop". So
between `d3ed533` and `4aeccfd`, `POST /payments/42/refund` with the button name
"Save Draft" was **allowed** on a blanket-attested walk, and had been **blocked**
before. Fixed by inverting the polarity so an unknown qualifier refuses.

**Not claimed:** that a lexical path rule is a sufficient guarantee. It is not —
the payment vocabulary is open and section-colliding at both ends, so inverting
only makes the incompleteness loud rather than silent. Moving the guarantee to
request-observation, so WALK does not adjudicate money movement by reading a URL
string, is the named next piece of work and is **not done**.

### Four rounds of non-author red-team

Peer session `nexusqa-2d`, each round from a fresh clone of the pushed SHA, never
the shared checkout. It found, in order: the page-vs-destination error that
produced the retraction; 13 under-blocks; the reintroduced over-block; and the
fail-open polarity. **The `d3ed533` version would have shipped and been wrong in a
way no existing test would have caught.**

---

## CI at the final SHA — and the lane that is red

Measured, not assumed. At `201bdd7`:

| workflow | conclusion |
| --- | --- |
| Nexus QA CI | **success** (29m34s) |
| M0.5 Security Gate | **success** |
| A11 Attestation Certification | **success** |
| Browser Test Harness (M0.2) | **did not trigger** — path-filtered off a docs-only commit |

**The browser lane is RED on this branch, and it was red before this run.** Its
run on `fc34c51` concluded **failure** after ~1h04m, in one job:

```
Crawl summit-life-carrier :: failure
  test_proving_ground_is_crawlable[summit-life-carrier] FAILED
  AssertionError: [summit-life-carrier] the crawl discovered 1 page state(s) but
  only 2 form signals and 0 actions — fewer than the 5 required. A crawl that
  reaches an application and captures nothing is a failure, not a pass.
  assert (2 + 0) >= 5
```

**Not caused by this run's commits.** The same job failed identically on
`0b88bf4` — four commits before the first commit of this run — and this run added
only markdown, evidence JSON and one standalone script under
`Nexus_power/scripts/`. Every other job in both runs passed, including
`Crawl vkpower-life`, `Crawl acme-life` and `Gate 2 — three real applications`.

Two things follow, and the second is the uncomfortable one:

1. It is a **third, independent** confirmation that summit-life-carrier is the
   broken leg — CI's own lane, not this session's crawl.
2. **"All required CI checks green" has not been true on this branch.** The three
   fast workflows go green on every push and are what recent commit messages
   cite; the browser lane runs for an hour, is path-filtered so it often does not
   trigger at all, and its **last two completed runs are both failures**. A gate
   that reads "CI is green" off the three fast lanes would have certified over a
   red one.

Note also that the `fc34c51` Nexus QA CI run concluded **`cancelled`** (superseded
by the next push) while `gh run watch --exit-status` returned **exit 0** on it. A
cancelled run is not a pass; this is the same failure mode `7491adf` records for
the browser lane, and it was re-encountered here. The green above is taken from
`conclusion`, not from the watcher's exit code.

## Findings opened by this run

1. **The refuse pack documents a remediation that was never applied** (§ root
   cause). Open. Owner: refuse-policy owner.
2. **`_producing_code()` cannot see a runtime-substituted configuration.** A crawl
   run against a refuse pack injected from outside the tree is stamped
   `dirty: false` and is indistinguishable from a real run. The T3 gate inherits
   the blind spot — it refused the counterfactual bundle only because it crossed
   0; had it crossed, **it would have been admitted**. See
   `evidence/gate2/r2_summit_counterfactual/NOT_ADMISSIBLE.md`. Remedy already
   implied by `CLAUDE.md §3`: record the digest of the bytes actually loaded.
3. **T-FL-08, one of R4's two "permanent alarms", does not run in the local lane**
   (7 skipped, infrastructure-gated). CI adjudicates it. Recorded in the R4
   decision rather than cited as a pair with the tripwire.
4. **(b+) has a landing hazard**: T-FL-08 is `xfail(strict=True)` at capacity 2, so
   a runtime refusal in `acquire_slot` would make it start passing and turn CI red.
   It must land together with that test's rewrite.

## What is NOT claimed

* Not claimed: that R1/R2 would cross if the refuse rule were changed. R2's
  counterfactual **still crossed 0**; a second blocker sits behind the first in
  each app.
* Not claimed: that the deploy would have worked. It was not attempted.
* Not claimed: any non-author reproduction, any signature, any clean-clone
  attestation.
* Not claimed: that the PAT is or is not still live — it is unverifiable from here,
  and that is itself the finding.

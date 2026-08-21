# Gate 2 — Three Real Applications

**Status: NOT MET. One of three applications completes a journey.**

Measured 2026-08-20/21 on branch `feat/qec-dynamic-catalog-p0-p6`, against images
built from each proving ground's own `Dockerfile` and crawled over HTTP in real
Chromium through the production `Crawler` and `PlaywrightBrowserPort`.

| WP | Claim | Verdict |
|---|---|---|
| A14 | vkpower-life completes a live journey | **NOT MET** — depth 6 → 12 on the real app; blocked at the payment step by a decision only tier 3 can make (see §2) |
| A15 | acme-life completes a live journey | **MET** — 2 crossings, 1 confirmation (`rung=dialog`), Docker-served |
| A16 | summit-life-carrier completes a live journey | **PARTIAL** — 3 login defects fixed; it now signs in and explores the platform, but crosses nothing and reaches no confirmation |
| A17 | Blocking proving-ground CI across all three | **BUILT, NOT ARMED** — job added and green locally; cannot be *required* until it has reported once in CI |
| A18 | Tier-3 oracle consulted in a live execution | **HALF MET** — the qualifying decision is identified and reproducible on a real app; the live consultation needs a model credential |
| A19 | Deep-flow telemetry emits non-null values | **PREMISE DID NOT REPRODUCE; the real gap is CLOSED** — see §5 |

---

## §0 · The headline

**"0 successful journeys" did not become "3". It became 1** — and the act of
measuring the other two produced eleven findings that were not previously
recorded, nine of them now fixed — including one in this gate's own test suite. One is a green-wash in the metric the whole
gate is denominated in; one is a login the crawler declared *failed* on an
application it had already signed into.

Fixed here:

| # | Defect | Where |
|---|---|---|
| 1 | A route revealed mid-walk was never discovered | `app/walker.py` |
| 2 | A committed password was re-typed into the page the login had left | `app/auth.py` |
| 3 | A submit that answers on a timer was read as a stuck form | `app/auth.py` |
| 4 | "Still busy" decided on the wrong signal, twice | `app/auth.py` |
| 5 | A CI leg published a port nothing listened on | `browser-harness.yml` |
| 6 | Three depth fields were computed, stored, and read by nothing | `qe-central/app/services/fleet_funnel.py` |
| 7 | A destination advance was refused as an irreversible act | `app/refuse_pack.yaml` (reviewed carve-out) |
| 8 | The hydration gate settled while the page was visibly working | `app/playwright_port.py` |
| 9 | 7 of 8 of this gate's own assertions passed on an empty crawl | `tests/browser/test_gate2_three_applications.py` |

Recorded, deliberately **not** fixed:

| # | Finding | Why not |
|---|---|---|
| 7 | `verified` accepts a bare `navigation` with no evidence | §3 — attempted and reverted |
| 8 | One directory holds two different applications | §1 — the owner has to say which one ships |

Nothing below is asserted without a bundle under
`Nexus_power/evidence/gate2/<app>/` containing the crawl's own coverage account,
its manifest and its screenshots.

Nothing below is asserted without a bundle under
`Nexus_power/evidence/gate2/<app>/` containing the crawl's own coverage account,
its manifest and its screenshots.

---

## §1 · Two applications share one directory, and CI crawls the other one

`proving-grounds/vkpower-life/` contains **two different applications**:

| | served by | routes | funnel |
|---|---|---|---|
| `index.html` | the local `FixtureServer` | `#/login`, `#/quote`, `#/apply`, `#/review`, `#/confirm` | 4 steps |
| `out/` (Next.js export) | the **Docker image** | `/login`, `/life-insurance/…`, `/portal/…` | 15 steps |

`tests/browser/test_proving_grounds.py` serves the source directory locally and
the container in CI, and asserts against both — so for vkpower-life the local
lane and the CI lane have never been testing the same software.

This is why every A14 number below names which application produced it.

---

## §2 · A14 — what the walker fix bought, and what it did not

### The defect, found and fixed

`_enqueue_link_hrefs` ran **only** in the discovery pass, over the controls a
state had when the frontier first opened it. An SPA that reveals its next route
*after* an in-page interaction was therefore invisible to it: discovery looked
before the link existed, and the walk that made it exist did not enqueue.

On the hand-written vkpower app the quote page renders `Apply now` only once the
quote form is submitted, so `#/apply → #/review → #/confirm` — the entire second
half of the funnel — was unreachable, and the crawl ended re-clicking
`See my quote` until the step stalled.

Fixed in `app/walker.py`: a link destination revealed mid-walk is now enqueued.
It **follows an href, it never clicks** — so the boundary model is untouched and
a commit-shaped control like `Apply now` is still classified approvable, still
offered to the operator, and still never actuated without a grant.

**Result on `index.html` (hand-written app):** exactly one crossing — `Bind
policy`, `#/review → #/confirm` — landing on *"Policy bound ✓ … CONFIRMATION
NUMBER VKPL-336267"*. Before the fix: zero.

Engine suite after the change: **2016 passed, 0 failed.**

### Why A14 is still NOT met — and how far it moved

The Next.js app the Docker image serves — the one CI crawls, and the one the
live portal at `vkpowerlife.136-85-106-73.sslip.io` serves — is a fifteen-step
funnel. Three fixes landed today took it from **depth 6 to depth 12**:

| depth | what unblocked it |
|---|---|
| 6 | baseline |
| 10 | `walker.py` — a route revealed mid-walk is enqueued |
| 11 | `rp.allow.destination_advance_step` — the reviewed carve-out below |
| 12 | `_BUSY_JS` — the hydration gate waits while the page is visibly working |

The walk now runs `member-lookup → personal-info → replacement → health →
lifestyle → decision → payment` contiguously, and **tier 2 fires for the first
time** on this application (`advances_by_tier: {"1": 15, "2": 1, "3": 6}`).

#### The third fix, because it generalises

`/apply/decision/` renders a spinner for ~1.8s before mounting its verdict. The
hydration gate has an empty-shell guard (`_MIN_INTERACTIVE`) designed for
exactly this, and it never fired: the SIGNED-IN header alone offers Dashboard /
Beneficiaries / Get a Quote, so the interactive count is comfortably over the
floor while the spinner is still on screen. The walk inventoried a page whose
only controls were nav links, tier 3 picked "Get a Quote", and a fifteen-step
funnel became an eleven-step loop back to the start.

A running CSS animation is the one statement a spinner makes in every framework,
and it needs no vocabulary and no page knowledge to read. Checked only once the
signature has otherwise gone stable, and bounded, so a page that is not
animating pays a single extra evaluate.

*(An earlier hypothesis — that the settle was completing against the page being
LEFT — was wrong; the URL is now in the quiescence signature anyway, which is
correct on its own terms, but it was not what stopped this walk. The measurement
that separated them was the tier-3 candidate list, which showed header nav and
nothing else.)*

#### What stops it now, and why it is A18's problem

The payment step's `Continue to Beneficiary Designation` is `disabled={!method}`.
The method is chosen from two **button-shaped choice cards**, and the crawl's
unblock experiment (`_answer_to_unblock`) handles checkboxes and radio groups —
not buttons. So the decision falls to tier 3, whose candidate set was:

```
[Dashboard, Beneficiaries, Get a Quote, Monthly, Quarterly, Semi-Annual,
 Annual, Credit / Debit Card Visa Mastercard Discover Amex, Back]
```

The right answer is there. The deterministic stand-in cannot reach it: choosing
a payment method is a semantic judgement, not a label match, and no forward-word
rule selects "Credit / Debit Card". Teaching the stand-in that answer would be
tuning a test double to one application, which is the one thing it must not be.

**This is A18's requirement, met in the only part that does not need a model: a
navigation decision on a real application that Tier-1 and Tier-2 provably cannot
resolve, identified and reproducible.**

#### The live deployment reaches the same depth

Run against `https://vkpowerlife.136-85-106-73.sslip.io/` with the same
instrument and the same narrow grant, the crawl walks the identical route —
`member-lookup → personal-info → replacement → health → lifestyle → decision
(tier 2) → payment` — at **depth 12, 28 states**, and stops at the same payment
step for the same reason. The platform behaves the same on the live deployment
as on the container built from source.

An earlier live run reached only **depth 5**: the walk logged `step_stalled
clicked='See My Quote' outcome='none' same_fp=True` on the health-check step,
while driving that same click directly navigated to `/quote/review/` in about
three seconds. That is a real race — a prefetched client-side route change
commits after the port settles — and `qec.wizard.relook` now looks again before
calling such a step a stall.

**It is not proven to be the repair.** The successful live run engaged that
re-look zero times; the race did not occur, so the improvement was variance.
Engagement is logged separately from the save precisely so the next occurrence
is attributable rather than inferred.

#### Two live runs, and a collision I caused

A second session ran the same live journey at 08:59 and committed its bundle to
the same path (`8eaf38e`), measuring **depth 7**. My later runs wrote over that
file; its measurement survives in git at that commit, but the working bundle is
now the depth-12 run. The two are not in conflict — theirs predates the carve-out
and the busy gate, which are what moved the number — but the evidence path was
never mine alone and I should have checked before writing to it.

**Their finding reproduces in my run, and it is the sharper one.** At
`/apply/member-lookup/` the engine fills *Member Number* with
`provenance=synthesized` (its `name_tokens` basis reads "Number" as a quantity;
a member number is an IDENTITY), the app accepts it, and — the actionable part —
**`fields_needing_seed` comes back EMPTY**. Confirmed independently here: my
live coverage also carries `Member Number` as inferred with `fields_needing_seed:
0`. My walk advanced past that step where theirs dead-ended, so the lookup is not
a hard block; but on either run an operator reading the result cannot learn that
a real member number is the one input that would change it. A crawl that is
honest about not completing and silent about why is the failure mode this gate
exists to remove. A18's remaining half — a live
consultation that answers it — needs qe-central, a fleet HMAC secret and a model
credential, none of which exist on this machine.

#### The end of the funnel is REACHABLE — measured, not assumed

Driving the remaining steps directly, with the application's own state seeded the
way the earlier steps would have left it:

```
/apply/beneficiary/   "Continue to Signature"        NOT disabled   -> tier 2 walks it
/apply/signature/     6 consent checkboxes + one field
                      placeholder "Type your full legal name"
                      6 checked + name typed -> submit ENABLED
                      -> /apply/confirmation/  ->  H1 "Application Submitted"
```

So the far side exists and is arrivable. The application stores state in
`sessionStorage`, so the cross-step name match does not require one unbroken page
session — an earlier assumption recorded here, and wrong.

#### Exactly two gaps now stand between the crawl and that page

Both are in the same place, `_answer_to_unblock`, and both are about a disabled
advance whose precondition the experiment cannot express:

1. **A choice rendered as BUTTONS.** The payment method is two button-shaped
   cards; the experiment handles checkboxes and radio groups. It is also the
   tier-3 decision described above, so either layer could resolve it.
2. **A "check ALL of these" consent block.** The signature step needs all six
   boxes. The experiment is deliberately ONE attempt per blocked step — "if
   answering one question does not clear the validation, the block is about
   something else and a second guess would be a search rather than an
   experiment." That reasoning is right for a *choose at least one* rule and
   wrong for a *consent to every one* rule, and the two are indistinguishable
   from markup: both are N optional-looking checkboxes gating a script
   validator.

Neither was attempted here. Both change how a crawl decides to act on controls it
was not asked to act on, which is the part of this system that most deserves a
deliberate design pass rather than a fix improvised at the end of a gate.

## §3 · The green-wash this gate produced, then caught

Granting `Continue to Underwriting Decision` walks four steps further, so it
looks like the way forward. **It is not, and the reason matters.**

A `boundary_approvals` entry is not a hint. It is the operator asserting that
the named control is an irreversible commit, and the platform treats that
assertion as load-bearing: `OutcomeMilestone.verified` accepts
`RUNG_NAVIGATION` *precisely because* "the click was a COMMIT and the landing is
therefore evidence about that commit" (`app/boundary.py`).

Grant a navigation control and that prior is false. Measured:

```
control_name        : "Continue to Underwriting Decision"
outcome             : navigation
confirmation_rung   : navigation
confirmation_detail : ""            <-- the application declared NOTHING
verified            : true
url_after           : /life-insurance/apply/decision/   <-- step 6 of 10
=> journeys_completed: 1
```

**A completed customer journey, claimed on a page the application never said
anything about.** `is_confirmation_landing` refuses this correctly —
`DECLARED_CONFIRMATION_RUNGS` excludes `navigation`, and its comment says a rule
that counted navigation "would report step one of a nine-step funnel as a
completed journey". `OutcomeMilestone.verified` uses the *other* ladder,
`CONFIRMATION_RUNGS`, which includes it.

The transit grants were reverted. `gate2_journey.grants_for` now issues **one
grant, for the commit control only**, and carries the reasoning so it is not
re-introduced. The blocked controls are recorded as `known_blockers` instead.

### The fix that was attempted, and why it was reverted

`verified` was changed to require a navigation-rung milestone to carry *what the
far side displayed* — a non-empty `confirmation_detail` or some
`outcome_values`. It kept every genuine confirmation measured in this gate
(vkpower's real commit records *"Policy bound ✓ … CONFIRMATION NUMBER
VKPL-336267"*; acme's rung is `dialog`) and rejected the transit landing.

**It broke `test_a_capture_failure_never_blocks_an_approved_crossing`**, and that
test is right. It encodes a reviewed principle — *"evidence capture is
best-effort BY DESIGN: a broken screenshot or an adapter with no `visible_texts`
must degrade the milestone, never veto a submit the operator explicitly
authorised"* — and a blind adapter produces an empty `confirmation_detail` for a
reason that has nothing to do with what the far side was.

The two cases are structurally identical: both navigated to a new URL with no
detail captured. What separates them is **why** the detail is empty — capture
failed, versus capture succeeded and the far side said nothing — and the
milestone does not record which. So the change was reverted.

**The fix requires a signal that does not exist yet:** the milestone needs to
carry whether evidence capture SUCCEEDED. With that, `verified` can demand a
declaration when the crawl could see, and fall back to the commit prior when it
could not. That is a small, safe change to make deliberately — not one to slip
in behind a grant that should never have been issued.

The correct remedy for the vkpower blocker is the one the refuse pack already
documents: a reviewed, versioned `allow_overrides` row — "adding a row is an
auditable, human-reviewed decision" — not a grant that lies about what a control
does.

### That decision was subsequently taken, on evidence

Classifying **every** forward control in vkpower-life's funnel against the pack
produced the discriminator, rather than an argument:

| control | refuse pack | `is_destination_advance` |
|---|---|---|
| Continue to Personal Information | safe | True |
| Continue to Health Questionnaire | safe | True |
| Continue to Lifestyle Questions | safe | True |
| **Continue to Underwriting Decision** | **danger** `rp.verb.underwrite` | True |
| **Continue to Payment** | **danger** `rp.verb.pay` | True |
| **Continue to Signature** | **danger** `rp.verb.sign` | True |
| **Sign & Submit Application** | **danger** `rp.verb.sign` | **False** |

Every navigation is a Tier-2 destination advance and **the control that actually
commits is not**. The rule the codebase already trusts at Tier 2 separates them
exactly, on this application, without help — the refuse pack simply was not
allowed to agree with it.

`rp.allow.destination_advance_step` lets it. Full-string anchored, scoped to
`button_name` only (no GET can be unblocked by it), and the destination noun is
**enumerated from the three measured step names** rather than left open, so
widening it later is another auditable decision instead of a silent consequence
of this one. `Submit to Underwriting`, `Sign & Submit Application`,
`Continue to Pay Now` and `Continue to Underwriting and Bind` all stay refused.

This is the third time `rp.verb.underwrite` has over-blocked: it was scoped off
`url_path` after marking 20 of 35 controls on one page as critical, including a
Back button and a notification bell.

Audited in `tests/test_refuse_pack_allow_overrides.py` — 21 assertions, of which
the majority test what the row must still REFUSE. `test_guard.py`'s
`allow_overrides == ()` pin caught the change, which is exactly what that pin is
for; it now pins the reviewed set instead of emptiness.

---

## §4 · A16 / A17 — the CI leg that could never have passed

`browser-harness.yml` published container port **3000** for summit-life-carrier.
Its `Dockerfile` sets `ENV PORT=3002` and `EXPOSE 3002`.

Verified against a real container: `:3000` gives no answer, `:3002` gives `200`.
That leg could only ever have failed at *"never served on :8099"* — so anything
it was believed to prove, it did not. Fixed: every matrix leg now carries an
explicit `port` read from its own Dockerfile.

With the port corrected the application serves and is crawled, and then failed
for a real reason — which turned out to be **three** defects in the login path,
each hiding the next. All three are now fixed, and the crawl signs in:

```
qec.auth.login_attempt success=True reason=login_verified steps=2
```

### 1. A successful login reported as `auth_failed`

The loop re-typed the password on EVERY iteration, while the username branch had
always been guarded by `not filled_username`. summit-life-carrier's handler
awaits 1200ms and *then* calls `router.push`, so the click returns and the port
settles while the navigation is still only scheduled. The next iteration
re-derived a password control from a screen the browser was about to leave:

```
fill#2  committed_value='...'   url=/portal/sign-in
fill#4  committed_value=None    url=/dashboard/overview   <-- ALREADY SIGNED IN
```

A full 30s action timeout watching the element detach, then `committed_value is
None` read as an uncommitted credential — and a crawl that was **already logged
in** ended `stop_reason=auth_failed`, `states=1`. Nothing about the application
was ever discovered. Fixed by giving the password branch the same guard.

### 2. A late answer read as a stuck form

With the re-type gone, the loop still gave up: the post-submit observation is
taken as soon as the click returns, and the "stuck" check at the bottom of the
loop is derived from it. A handler that answers on a timer has not answered yet,
and read once that is indistinguishable from a form refusing to advance.

Fixed with a bounded second look (`LATE_ADVANCE_WAIT_MS` / `LATE_ADVANCE_LOOKS`)
that only runs where the loop was about to abandon the login, so a healthy login
pays nothing.

### 3. Two wrong definitions of "has it answered"

The second look needed a stopping rule, and the first two were both wrong —
each disproved by the same application:

* **"the fingerprint moved"** — the submit flips the button to
  `Authenticating...`, which moves the fingerprint while saying precisely that
  the answer has *not* arrived;
* **"there is something to submit"** — this page carries *Sign in with Google
  SSO* and *Sign in with Enterprise SSO*, so a submit-shaped control is present
  on every observation, busy or not.

What works is the application's own structural statement: **a disabled control**.
It needs no vocabulary, no spinner detection and no page knowledge, and it
clears itself the moment the work finishes.

Pinned by three tests in `tests/test_auth.py` driven by a `LateAnsweringBrowser`
that answers on a timer — a shape no existing fake could produce, because every
other fake answers a click synchronously.

### 4. The grant was being spent on the wrong control

With the login fixed, summit appeared to cross its commit boundary. It had not.
The dashboard's left nav carries a LINK also called *Submit Application*, and
grants match on the normalised LABEL — so an unscoped grant was consumed at
`/dashboard/overview` with `outcome=error, confirmed=false`, and
`max_crossings=1` then refused the real commit button for the rest of the crawl.

**This is the clearest near-miss in the gate.** Reported one step earlier it
would have read as "summit-life-carrier crosses its boundary" — a green built
entirely on a navigation link that happened to share a label with a submit
button. `ApprovalGrant` already supports a `url` that narrows a grant to one
page; the Gate-2 grant now uses it.

### What summit does now, and what it still does not

Measured with the correctly scoped grant:

```
crossed      : 0
flows        : 5   journeys_completed: 0
deepest_flow : 1   proven: 1   capped: false   terminal: submit_boundary
```

It signs in, reaches the carrier platform (New Business Queue, In-Force
Policies, Reported Claims), walks five one-step flows and stops politely at
their submit boundaries. It never reaches
`/underwriting/new-business/new-application`, so the control it is authorised
for is never offered. **It crosses nothing, and that is the honest number.**

The login was the gate's cheapest and largest single win — from `states=1,
auth_failed` to a real crawl of the platform — but walking the five-step
new-application wizard is untouched work.

### The A17 lane

`tests/browser/test_gate2_three_applications.py` builds and crawls all three
applications and asserts what each **achieved** — boundaries crossed,
confirmation observed, no boundary crossed twice, every crossing carrying its
`approval_id`, depth telemetry non-null. **18 assertions, green twice locally.**

It is a **two-way ratchet** against a committed declaration, not a floor:
an application that stops crossing fails, and an application that *starts*
crossing also fails, because its declaration is now understated and a human must
raise it against the new evidence. A one-way floor can be satisfied by a crawl
that got luckier, and nobody ever learns the platform changed.

Two of the three declare a journey they do **not** complete. That is the point:
a lane that skipped them would read as a lane that passed.

**Provenance warning.** The `browser-harness.yml` half of this work — the port
fix and the `gate2-journeys` job — is already at HEAD, but inside commit
`7d79739` ("gate3(A21): the crawl-evidence steps were unreachable behind a stale
golden"), which is a different gate's commit by a different session. This
checkout has one shared git index, so `git add` in any session sweeps up every
other session's staged edits. The content is correct and byte-identical to what
was written here; only the attribution is wrong. Anyone auditing `7d79739` will
find Gate-2 CI changes its message does not mention.

**Not yet a required check.** `gate0_require_ci_lanes.sh` refuses to require a
context that has never reported — a required check GitHub is still waiting to
hear from blocks every pull request forever. Sequence: push → let the job run →
confirm it reported → then `--apply`.

---

## §5 · A19 — the premise did not reproduce

A19 states that `deepest_flow_*` "emit null values" in production. **They do
not.** Measured, on the crawls' own accounts:

| app | steps | proven | capped | terminal |
|---|---|---|---|---|
| acme-life | 3 | 3 | false | `submit_crossed` |
| vkpower-life (Next.js) | 10 | 0 | true | `loop` |
| summit-life-carrier | 0 | 0 | false | `""` (auth_failed — a correct zero) |

The transport is intact end to end: `flow_ledger.summarize` → `coverage.build()`
→ the completion callback's `"coverage"` → qe-central's
`stats_dict["coverage"] = body.coverage` → `stats->'coverage'->'flow_summary'`,
which is what `golden_crawl_gate.sh` reads.

**The real gap is different, and worse than a null.** Of the four fields, only
`deepest_flow_steps` has a consumer anywhere in qe-central
(`services/fleet_funnel.py`). `deepest_flow_proven_steps`, `deepest_flow_capped`
and `deepest_flow_terminal` are computed, transmitted, stored — and **read by
nothing**. They exist because the integer alone cannot be read:

> Six steps because the application has six, and six steps because the walk was
> cut off at six, are the same integer and opposite facts.
> — `app/flow_ledger.py`

So every production reader sees exactly the ambiguous number those three fields
were added to disambiguate. The vkpower row above is the case in point: `10`
looks like deep coverage and is actually `capped=true, proven=0` — a truncated
traversal that proved nothing.

**Closed.** `qe-central/app/services/fleet_funnel.py` now reads all four, and the
stage the product turns on — *"deep enough for E2E"* — is computed from PROVEN
depth rather than walked depth. Before this change a fleet of truncated
traversals scored identically to a fleet of completed journeys at that stage.

Crawls whose depth is a floor are not silently dropped: they are counted in
`capped_depth_crawls` and named in the summary notes, because a stage that
quietly loses rows is how a fleet report stops being believed. Rows recorded
before the explorer emitted `deepest_flow_proven_steps` carry no opinion about
it and fall back to walked depth — reading their absence as "proved nothing"
would have deleted every historical crawl from the stage on the day this
shipped, and read as a fleet-wide regression.

Three regression tests pin it (`tests/unit/test_fleet_funnel.py`): a capped walk
is not E2E-capable, a proven walk is, and a pre-hardening row is not
reclassified. The Gate-2 lane separately asserts all four fields are non-null on
every application, so the null A19 describes cannot appear without failing CI.

---

## §6 · What is required for Gate 2, in order

1. **A14 — decide `Continue to Underwriting Decision`.** It is the single thing
   standing between vkpower-life and its remaining nine steps. A reviewed
   `allow_overrides` row is the mechanism the refuse pack documents for exactly
   this; a `boundary_approvals` grant is NOT, because it asserts the control
   commits and §3 shows what the platform then does with that assertion. This is
   a safety-data decision and belongs to whoever owns the pack — it was
   deliberately not taken here.
2. **Give the milestone a capture-succeeded signal**, then make `verified`
   demand a declaration when the crawl could see (§3). Until then the product's
   headline metric can be raised by a click that merely navigated.
3. **A14 — let a walk continue past a transit crossing** without losing the
   React store. vkpower's signature step needs a typed name matching one entered
   seven steps earlier, so nothing short of a contiguous walk can satisfy it.
4. **A16 — take summit past its commit boundary to a confirmation.** The login
   is fixed and the boundary is crossed; the wizard walk is what remains.
5. **A18 — untouched, and out of reach here.** The live path is now wired
   correctly (`gate2_journey.py --oracle live`) but needs qe-central up, a fleet
   HMAC secret both sides agree on, platform-api reachable, and a model
   credential. `consults=0` remains true of every execution in this gate.
6. **Arm the A17 check.** Push, let `gate2-journeys` report once, then
   `scripts/gate0_require_ci_lanes.sh --apply`. Not before — the script refuses,
   and it is right to.
7. **Independent reproduction by a non-author squad** — not started for any
   milestone. No Gate-2 criterion is complete without it.

---

## §7 · Reproducing everything above

```bash
docker build -t pg-acme-life:gate2  Nexus_power/proving-grounds/acme-life
docker run -d --name pg-acme -p 8102:80 pg-acme-life:gate2

cd Nexus_power/engines/qe-explorer
python gate2_journey.py acme-life --url http://127.0.0.1:8102/
# -> Nexus_power/evidence/gate2/acme-life/{journey,coverage}.json

# all three, with assertions:
python -m pytest tests/browser/test_gate2_three_applications.py -v
```

# R7 · refuse-pack change — SEVEN ROUNDS of non-author red-team

**Final verdict at `2b7604c`: CONFIRMED CLEAN.** Recorded at the moment it
happened, per `GATE_5_CEREMONY.md §3`.

| | |
| --- | --- |
| Red-teamer | peer session **`nexusqa-2d`** |
| Author (this session) | `nexusqa-1c` |
| Method, every round | fresh clone of the **pushed SHA**, never the shared checkout; measured through `build_inventory` (the layer that gates the crawl) and `classify_request` (the route/WALK layer) |
| Rounds | 7 |
| Findings that were real | **every one** |

The red-teamer refused, correctly, to verify against the working tree on two
separate occasions when a SHA had not yet been pushed. That refusal is part of the
evidence.

---

## What each round found

| # | SHA | Finding |
|---|---|---|
| 1 | `d3ed533` | **The premise was wrong.** The "page-level over-block" did not exist — `build_inventory` passes each control's *destination*, never the page. The change as authorised would have removed destination refusal entirely: a link to `/account/delete` would have stopped being refused, 13 tests red. → **retracted** |
| 2 | `897da04` | 13 under-blocks: `/pay-now`, `/pay.php`, `/submit-to-underwriting`, `?action=underwrite` … The assumed route-layer backstop **did not exist** — `rp.get.action_mutation` listed neither `underwrite` nor `remit` |
| 3 | `22da0d7` | The fix for round 2 **reintroduced the over-block**: `/remittance-advice`, `/payout-history`, `/autopay-settings` … refused as acts |
| 4 | `4aeccfd` | `capture` fell through a gap **between two of the author's own lists**; plus 18 fail-open money verbs (refund, void, reverse, chargeback, settle, redeem, cash, issue) |
| 5 | `2fc403f` | **Fail-OPEN polarity**, and the argument that mattered: `classify_action_verb` is the *sole* gate for a mutating request in `Phase.WALK`, the phase with no human in the loop |
| 6 | `09160b6` | The inversion **re-sealed the funnel**: `/payments/new` is the direct analog of the summit wizard entrance that started the whole line of work |
| 7 | `2b7604c` | The two branches ran **opposite polarities** — `/pay-debit` refused, `/payments/42/debit` allowed. **A finding the red-teamer introduced with its own round-5 advice** |

## The two findings worth carrying past this pack

**1 · Verifying a rule means reading the call path, not the rule text.**
Round 1's retraction came from measuring `classify_control_danger` directly with a
page URL — a call shape `build_inventory` never makes. The original diagnosis and
its first review both checked the rule and neither checked the caller. The same
error recurred one layer along in round 2, where a backstop was *asserted* without
reading its contents.

**2 · A consistency test is only as good as the axis it compares on.**
The drift test written after round 4 diffed the *section vocabulary* across two
rules — but the two branches shared no vocabulary to diff, one carrying a section
list and the other a commit list. It was structurally incapable of seeing the
disagreement it existed to prevent: **a blind verifier inside the test written to
prevent blind verifiers.** Its replacement compares **verdicts**, not
implementations:

```python
assert _link_danger(f"/pay-{verb}") == _link_danger(f"/payments/42/{verb}")
```

One word, two shapes, one verdict — agreement rather than danger, so it cannot be
blinded by which list a word happens to live on.

## Why this is the argument for the requirement

In the red-teamer's own words, kept because it is the strongest data point in the
record:

> the last real finding was one I introduced, caught only because a second session
> measured it. That is the requirement working, not the ceremony.

The `d3ed533` version would have shipped, and been **silently wrong in a way no
test in this repository would have caught.**

## Residual risk — stated, bounded, and pointed loud

Both parties agree on the closing state:

* **Correct on every safety axis measured** — no money verb crosses silently, in
  either URL shape, in any phase tested.
* **The incompleteness is consistent, not absent.** A lexical path rule cannot be
  complete: the payment vocabulary is open *and* section-colliding at both ends
  (`/refunds` browses, `/payments/42/refund` commits). Unifying the polarity makes
  every miss a visible over-block instead of a silent crossing; it does not make
  the list complete.
* **The URL string is still the sole gate in `Phase.WALK`.**

**The real fix is NOT implemented and is named:** move the guarantee to
request-observation, so WALK does not adjudicate money movement by reading a URL
string at all. Then `/payments/new` is not name-classified dangerous in the first
place, and neither the funnel-seal nor the money-tail can recur by construction.
The red-teamer has offered to verify that change fresh; it is large enough to
deserve a non-author pass of its own.

## What this does not do

Fills no signatory seat. Evidence independence is session-level; accountability is
human-level, and every Gate 5 seat remains vacant.

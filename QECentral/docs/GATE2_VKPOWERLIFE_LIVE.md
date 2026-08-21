# Gate 2 — vkpowerlife, LIVE deployment: the journey is not yet actual

**Date:** 2026-08-21 · **Target:** `https://vkpowerlife.136-85-106-73.sslip.io/`
**Instrument:** `qe-explorer/gate2_journey.py vkpower-life --url <live>`
**Evidence:** `Nexus_power/evidence/gate2/vkpower-life-LIVE/`
**Authorisation:** full journey approved by the operator; test application.

## Verdict

**The end-to-end journey did NOT complete, and the run says so.**

```
boundaries_crossed    : 0
confirmation_observed : false
journeys_completed    : 0
commit_control        : "Sign & Submit Application"   <- never reached
approvable_boundaries_seen : ["Apply Now"]
```

This is a real result, not a harness failure: the crawl ran to
`stop_reason=completed` over 17 states / 86 actions with **0 guard blocks**, and
reached 9 distinct routes including authenticated areas.

> ⚠️ **The existing `evidence/gate2/vkpower-life/` bundle is NOT this.** Its
> `target_url` is `http://127.0.0.1:8101/` — a local replica. It crossed one
> boundary; the live deployment crosses none. Do not read the local bundle as a
> live proof. This bundle is kept separately for that reason.

## Where it got to

```
/
/portal/dashboard/              <- authenticated
/portal/beneficiaries/          <- authenticated ("Sign out" present)
/life-insurance/quote/start/    <- radio product gate, CLEARED (see A6, GATE1_EXIT_STATUS §5)
/life-insurance/quote/coverage/
/life-insurance/quote/personal/
/life-insurance/quote/health-check/
/life-insurance/quote/review/   <- "Apply Now" offered here
/life-insurance/apply/member-lookup/   <- TERMINAL STALL
```

7 tier-1 advances + 4 tier-3 oracle advances; `deepest_flow_steps: 7` but
`deepest_flow_proven_steps: 0`.

## Finding 1 — the app drops a verified login on a fresh page load

```
auth_blocked     : False
auth_incomplete  : True
auth_reason      : "not_persisted"
```

`not_persisted` is specifically **not** `session_expired`, and the distinction is
load-bearing: `auth_flow.py:604` selects it only when the login **verified this
crawl**. The crawl signed in successfully — it reached `/portal/dashboard/` and
`/portal/beneficiaries/` with a `Sign out` control present — and then met a
sign-in wall again on a fresh page load.

The engine's own words (`auth_flow.py:536`): *"the login verified but this app
drops it on a fresh page load (its session lives in the page, not a cookie)"*.
It responds correctly — raises the re-login budget and continues in place — so
this is diagnosed, not silently absorbed.

**This is an application property, not a crawler defect**, and it is the reason
authenticated depth does not carry into the application funnel.

## Finding 2 — the funnel dead-ends on a synthesized Member Number, and nothing says so

The terminal stall:

```
qec.wizard.gate_open   url=/life-insurance/apply/member-lookup/ filled=1
                       pick='Continue to Personal Information'
qec.wizard.step_stalled url=/life-insurance/apply/member-lookup/
                       clicked='Continue to Personal Informati'
                       outcome='none'  same_fp=True
```

The field it filled:

```json
{"name": "Member Number", "provenance": "synthesized", "semantic_type": "quantity",
 "basis": "name_tokens", "confidence": 0.72, "filled": true, "widget": "text"}
```

and the app's own declaration of it: `"required": false`.

So the crawl invented a value for **Member Number**, classified it as a generic
**`quantity`**, the application silently refused the lookup (no navigation, no
error, identical fingerprint), and:

```
fields_needing_seed        : []
fields_needing_seed_detail : []
```

**Nothing in the report tells an operator that a real member number is the thing
standing between this crawl and a completed journey.** That is the actionable
defect. A funnel-gating identity field that is synthesized and silently rejected
should be surfaced as needing a seed; here the run is clean, honest about not
completing, and silent about *why*.

Two contributing factors worth separating:

* `semantic_type: "quantity"` — a member number is an **identity**, not a
  quantity. The `name_tokens` basis matched "Number".
* `required: false` — the application does not mark the field required, so a
  requiredness-driven seed check cannot catch it either.

A member-data resolver exists in the platform but is flag-OFF; routing
identity-shaped fields to it is the obvious remedy and is not attempted here.

## What was done to the live deployment

| | |
| --- | --- |
| `forms_submitted` | **0** |
| `boundaries_crossed` | **0** |
| `unblock_irreversible` | **0** — no experiment left committed |
| `network_server_error_count` | **0** — caused no 5xx |
| `guard_blocks` | 0 |

The commit control `Sign & Submit Application` was **never reached**, so no
application was submitted. The grant existed and went unspent.

## Honest statement

Gate 1 made the journey *possible* and A6 is live-proven. Gate 2's claim — that
the journey is **actual** — is **not met on the live deployment**. It is blocked
by two specific, named things: an application that does not persist its session,
and an identity field the fill engine cannot invent and does not flag. Both are
now measured rather than suspected.

# Execution Reliability Doctrine

**Founder-locked, 2026-07-24.** Born from the first client-visible product
failure (run `7c89de7e`: a generated oracle `getByText(/https/i)` — text no
page ever renders — failed step 7 and the report implicitly blamed the
client's application). This document is the standing law that makes that
class of event structurally impossible to repeat, and the honest process for
every class we have not met yet.

## The two sentences

> **1. A generated test must prove itself on the baseline before it may judge
> the application.**
>
> **2. Blame requires positive evidence; "not yet attributed" beats a guess —
> in both directions.**

## The five layers (all live)

| Layer | Mechanism | Where |
|---|---|---|
| **0. Proven-oracle policy** (default ON) | Prose-derived text oracles are non-fatal; every miss is **recorded** (`__nxSoftMiss` → reporter annotation → ingest metadata → visible warning). Grounded oracles (recorded fills, navigation, values) stay hard. `NEXUS_PROVEN_NAV_ORACLE=0` restores legacy hard mode. | `script_factory/compiler.py` |
| **1. Certification-before-client** | Every generate/regenerate dispatches one run of the suite against the app's own baseline, tagged `environment='certification'`. Certification runs never appear in client-facing stats. A product/unproven certification failure **quarantines** the case from client runs until it re-certifies. The first failure of a defective script is OURS. | `routers/test_factory.py::_certify_generated_suite`, `services/test_runs.py` |
| **2. Pre-run auditor gate** | Deterministic dimensions (impossible navigation, `V_URL_TEXT` URL-as-text oracles, ambiguity, dead scaffolding) with `NEXUS_AUDITOR_GATE=block`. Every escaped class becomes a new dimension. | `test_factory/playwright_auditor.py` |
| **3. Attribution Engine** | Deterministic ladder at ingest: product / application / environment / configuration / test-data / **unknown** — every verdict carries verbatim evidence quotes; an application claim requires grounded evidence (5xx, a PROVEN oracle breaking). Unattributed failures render as *"cause under analysis"*, never implicit app blame. | `test_factory/attribution_engine.py` |
| **4. Golden-corpus CI** | Three recorded-substrate corpora run generate→compile→audit on every push; invariants I1–I6 (business names, gate-pass, no URL oracle, no URL prose, no silent soft-swallow, brace-balance) fail OUR build before a deploy. | `tests/test_golden_corpus_generation.py`, `qec-ci.yml` |

## The escape→guard law

Every defect class that ever reaches a client MUST gain, before its fix is
considered done:

1. a **guard** (compiler/generator fix, auditor dimension, or attribution rung),
2. a **regression test**, and
3. an entry in `ESCAPED_DEFECT_REGISTRY` (`attribution_engine.py`).

`tests/test_escape_guard_registry.py` makes the ledger unfalsifiable: an entry
without an existing test fails CI; the founding entry can never be deleted.

## The north-star metric

**Client-visible product-fault failures — target: zero.**
`GET /api/v1/test-factory/{artifact_id}/quality/product-faults` reports it,
alongside `caught_in_certification` (the gate doing its job). The Studio shows
both on the Discovered Flows board. Review weekly; every non-zero is an
escape→guard cycle.

## The don'ts

- **Never auto-retry to green.** Retries convert real regressions into
  "flaky" — green-wash with extra steps.
- **Never heal an oracle.** Heal is for locators; a wrong oracle is
  regenerated honestly or removed with an UNVERIFIED note.
- **Never let an explanation ship without a verbatim evidence quote.**
- **Never promise zero failures.** Promise zero client-visible product-fault
  failures and zero unattributed blame. An application failure caught with
  proof is the product working.
- **UNKNOWN is a legitimate verdict. A guess is not.**

## Quarantine rules (blame and quarantine are separate decisions)

| Certification outcome | Attribution | Quarantined? | Why |
|---|---|---|---|
| failed | product / unknown / none | **YES** | not proven runnable; product-side until proven otherwise |
| failed | application | no | a grounded regression on the baseline is the client's signal — hiding it is green-wash |
| failed | environment / configuration | no | outages never shame the cases |
| certified / none | — | no | — |

One pure function (`test_runs.quarantine_decision`) implements this for both
the run-gate and the UI, so they can never diverge.

# Findings — platform/api regressions surfaced by wiring the suite into CI

**Date:** 2026-07-21 · **Branch:** feat/qec-phases-0-6 · **Severity:** 1 CRITICAL, 6 HIGH
**How found:** Gap G3 (audit) asked us to run the platform/api tests in CI — they had
never run there. Running them surfaced **8 currently-failing tests**. An adversarial,
git-bisected triage (18 agents, each finding independently verified) classified every
one as **stale-test** (fix the test) or **real-regression** (fix the code). This doc
covers the **real regressions** — code that is genuinely wrong at HEAD. The stale tests
were corrected in place (5 of them) and are green.

## Root cause (one commit, one mechanism)

All seven regressions trace to a single commit:

> **efd0269** — `feat(production): trust-track production sync from VM — pages&forms fix-set,
> suite generation, generation engine, verification platform, QE agents`

That commit **synced FROM the VM INTO the repo**, and in doing so overwrote several
platform/api files with an **older lineage** — silently reverting the Phase-0
never-green-wash auditor upgrade (shipped at **6bfcbad**, "Phase-0 never-green-wash
fixes") and re-introducing a FastAPI app-assembly bug. This is the repo↔VM divergence
risk realised as a regression: the sync treated the (older) VM code as canonical and
clobbered newer work. Because platform/api tests didn't run in CI, nothing caught it.

The tests are held visible as **strict `xfail`** (registry:
`platform/api/tests/conftest.py`). The moment the runtime is fixed, each flips to a real
pass — a strict xfail turns any xpass into a CI failure, forcing the stale marker out.

---

## CRITICAL — the platform app does not assemble from a clean build

**Tests:** `test_module_graph_smoke.py::test_platform_main_assembles_app`,
`::test_p3_p6_endpoints_registered`
**File:** `platform/api/app/routers/integrations.py` (DELETE `uninstall`, ~L452-461)

`integrations.py` has `from __future__ import annotations` (L30) and declares:

```python
@router.delete("/{integration}", status_code=204)
async def uninstall(...) -> None:
```

Under PEP 563 the `-> None` annotation is stringified to `"None"`. FastAPI 0.115.2
(within the SDK pin `>=0.109,<1.0`) resolves the ForwardRef `"None"` to `NoneType` (a
truthy class) and treats it as a **response_model**, then asserts a 204 must have no
body — raising at **route-construction time**:

```
AssertionError: Status code 204 must not have a response body   (fastapi/routing.py:507)
```

**Reproduced directly:** `python -c "import main"` raises the assertion; the whole app
fails to import, so **zero routes register**. This violates the Phase-0 exit criterion
"a clean build == what runs" — a clean build of platform/api won't boot. (It likely
still runs on the VM because that host pins an older FastAPI that tolerates it — which
is exactly the divergence trap.)

**Recommended fix (behaviour-preserving, one line):** make the no-body contract explicit
so FastAPI never treats the annotation as a model —
`@router.delete("/{integration}", status_code=204, response_model=None)` — or drop the
`-> None` return annotation. Then re-run `test_platform_main_assembles_app`.

---

## HIGH — the never-green-wash auditor upgrade was reverted (5 tests)

**File:** `platform/api/app/services/test_factory/playwright_auditor.py`
(efd0269 changed 254 lines here.)

The auditor is the gate that must never let a script certify on a signal it didn't earn.
The Phase-0 upgrade (6bfcbad) added two never-green-wash capabilities that efd0269
**removed**:

1. **Ambiguous-locator dimension** — `V_AMBIGUOUS`, `_ambiguous_labels()`, and
   `score_spec()` emitting an "Ambiguous locator (warning)" finding + per-step
   `V_AMBIGUOUS` verdict when a name-based locator (e.g. `getByRole('button',{name:'Add
   to cart'})`) matches repeated controls on the same page with no disambiguating anchor.
   **Confirmed gone:** `grep V_AMBIGUOUS|_ambiguous_labels|"Ambiguous locator"` = 0 hits.
   *The saucedemo "6× Add to cart" blind spot is silently certified again.*
   Tests: `test_auditor_flags_ambiguous_locator_without_anchor`,
   `test_gate_surfaces_ambiguous_but_does_not_block`.

2. **Honest gate reporting** — `gate(report, *, blocking=False)` returned
   `enforced` / `passed` / `would_block` / `block_reasons`, computing an HONEST
   `passed = not would_block` **independently of enforcement** (warning-only mode still
   reports the truth). At HEAD `gate(spec_text, steps, evidence=None, *, enforce=False)`
   re-scores internally, exposes no `enforced`/`block_reasons`, and couples the block
   signal to enforcement (`would_block = enforce and not passed`).
   Tests: `test_gate_default_is_warning_only_but_honest`, `test_gate_passes_a_clean_report`,
   `test_gate_blocking_rejects_impossible_transition` (this one's core block-impossible
   behaviour survives, but the structured `block_reasons` API it asserts regressed).

**Recommended fix:** restore the 6bfcbad `playwright_auditor.py` auditor (ambiguous
dimension + report-consuming `gate(report, *, blocking=)` with honest independent
`would_block`), reconciling any intended non-auditor changes efd0269 bundled. This is a
"which lineage is canonical" decision — **founder call**, since efd0269 was a deliberate
production sync.

---

## HIGH — audio_intent_match contract broken (1 test)

**Test:** `test_action_extractor.py::test_reconcile_corroborates_value_via_ocr_and_control`
**File:** `platform/api/app/services/storyboard/action_extractor.py` (`_reconcile`, ~L631-633)

The `ExtractionEvidence.audio_intent_match` contract (schemas.py L220-228) documents:
narration "now I select Texas" + LLM `verb=select, value=TX` ⇒ `audio_intent_match=True`
(match on the value/verb combo). But `_reconcile` checks
`action.target_label.lower() in inputs.audio_intent.lower()` — it requires the field
**prompt** ("What state do you live in?") to be a substring of the narration, which real
narration never contains. So the signal is `False` in exactly the case the contract says
is `True`.

**Recommended fix:** match the transcript against the action's **value/verb** (per the
documented worked example), not `target_label`. Then re-run the test.

---

## What we did NOT do, and why

We did **not** patch any of this runtime. platform/api is the frozen VKPower factory and
efd0269 was an intentional VM sync — re-diverging the repo could re-open the very
divergence the sync closed, and choosing the canonical lineage is a founder decision. We
have instead: (a) fixed the 5 genuinely-stale tests, (b) pinned the 7 real regressions as
documented strict-xfail so CI runs green **and** the regressions stay visible, and (c)
written this doc with exact fixes. Un-freeze + apply in one reviewed pass whenever you're
ready; each xfail will flip to green and prove the capability restored.

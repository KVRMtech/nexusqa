# Nexus QA — Defect Register (2026-06-27)

Source: 106-agent grounded defect hunt (12 specialist finders → adversarial verify → synthesis).
90 candidates → **80 confirmed → 38 deduped**: showstopper 4 · high 18 · medium 13 · low 5.

> **Headline:** Never-green-wash is breached on the PRIMARY run path and via unprivileged APIs; a single
> systemic root cause — **identity/state keyed on position, version-string, or model-name instead of stable
> content** — silently strips evidence and self-perpetuates.

## Systemic themes
1. **Never-green-wash breaches (most severe):** the honesty gate auto-heal enforces (hollow-suite refusal +
   `assert_assertions_unchanged` + 2× confirm) is **absent on every other write path** — bare run/ingest verdict,
   `scripts/save`, PATCH re-point, ground-truth ingest, single-step heal, commit-label/split-proof-gate.
2. **Missing RBAC on mutating/evidence routers:** `_rbac_gate`+`_audit_mutation` applied to test_factory but NOT
   storyboard or test_runs_feedback — a `viewer` can fabricate PROVEN ground-truth, inject a passing "Clean Run",
   plant proof screenshots, poison heal-capture, wipe recorder evidence, burn vision spend.
3. **Systemic identity/keying fragility:** `page_visit_id` position+version-keyed, `test_id` signature-keyed → any
   one-page extraction shift orphans Plane-B overrides, CASCADE-deletes approved scripts, orphans the action↔visit
   join (zeroing clicks/fills), discards paid vision enrichment — all silently. Version selection diverges
   (lexical `func.max` vs `created_at`) past v9.
4. **Silent-failure guard has holes:** `extraction_health`/`no_cases_reason` key on a weak-model regex + `total_actions>0`,
   so cloud-LLM 429/5xx + timeouts yield empty Pages&Forms reported healthy; degraded runs are version-stamped+frozen.
5. **Value/label collapse drops fills:** `_resolve_fields` de-dupes by shared value or loose prefix → drops a real
   fill when two fields share a value (two ZIPs, confirm-password) or a prefix label.
6. **UI verdict mis-attribution & state loss:** "This run" shows the global-newest run; console/trace/approval state
   wiped on refresh; View-code shows generated (not active healed) source.
7. **PII egress & token hygiene break the on-prem promise:** raw SSN/DOB frames go to cloud LLMs unredacted; export
   defaults to raw PII; full session JWT accepted via `?token=` on all routes/methods.

---

## SHOWSTOPPERS

### 1. `/ground-truth` ungated → viewer injects fabricated PROVEN (conf 1.0) page-visits + wipes recorder evidence
*green_wash + security_rbac.* storyboard router (storyboard.py:38) has NO `_rbac_gate`; `ingest_ground_truth`
(storyboard.py:473) runs on bare `get_current_user` (default role `viewer`). Client events → GROUND_TRUTH →
`_confidence_for_source=1.0` (page_visit_extractor.py:1442) → `_page_proven` True → hard `toHaveURL`. Override
variant (page_visit_extractor.py:1689) keeps `prev_navigated=True` and asserts the fabricated path. Handler DELETEs
all prior `GroundTruthEventRow` before insert (empty body wipes evidence). Intra-tenant.
**Fix:** mount storyboard_router behind `_rbac_gate` (admin|manager for mutations); ownership check before delete+insert;
treat client ground-truth as a LOWER trust tier (INFERRED) unless it joins a sampled frame with an independent
corroborating signal + signed recorder attestation — never auto-grant conf 1.0/PROVEN to a client-writable source.

### 2. `/test-runs/ingest` ungated + status-less step = PASSED → viewer fabricates a passing "Clean Run"
*green_wash + security_rbac.* test_runs_feedback_router (main.py:277) has no `_rbac_gate`. `IngestStep.status`
defaults `Field('passed')` (test_runs_feedback.py:82, no enum); `ingest_run` does `(s.get('status') or PASSED).lower()`
at test_runs.py:158 AND :215, so absent/empty status → PASSED — the `or` fires BEFORE the broken-coercion check at
:216-220 (unknown string fail-safes to `broken`, but absent/empty fails toward green). Run = PASSED when passed>0,
failed==0 (:172-175).
**Fix:** RBAC/CI-service-token gate the ingest router; make `IngestStep.status` required/Literal-validated; coerce
missing/empty to `broken` at BOTH :158 and :215 BEFORE the membership check; flag client-ingested runs and never let
one satisfy a server-execution oracle gate.

### 3. Normal run verdict = `VERDICT_PASSED 1.0 "Step passed."` with NO outcome-grounding (gate only in auto-heal)
*green_wash.* test_runs.py:713-714 `if not failed: return Verdict(VERDICT_PASSED, 1.0, 'Step passed.')`; `failed`
is pure step status (:979), no grounded-oracle check. The hollow-suite gate (`self_heal.suite_outcome_grounding`)
runs ONLY in the heal loop (test_factory.py:1926-1936); the primary Run→ingest→`_timeline_for_run`→`classify_failure`
never applies it.
**Fix:** apply `suite_outcome_grounding` in `_timeline_for_run` when `failed=False`: a hollow scenario (no grounded
outcome oracle on any executed step) → `green-but-not-grounded`/needs_review, never conf 1.0 "Step passed." Reuse ONE
honesty gate across run + heal; make "hollow" proportional (grounded-fraction floor) so 1-of-20 doesn't freeze green.

### 4. `scripts/save` activates arbitrary human Playwright with ZERO oracle/content validation
*green_wash.* `save_version` (test_factory.py:3188) calls `save_new_version` without `proposed=True`
(versions.py:204), so the source becomes the highest ACTIVE version (`_active_edited_map`→`_configured_files`). No
`assert_assertions_unchanged`, no grounded-assertion check, no compile sanity. Heal path is forced through
`assert_assertions_unchanged` + persists proposed=True; manual save honors neither.
**Fix:** run save source through `assert_assertions_unchanged(compile_case(tc,field_meta), body.script_source)` and
refuse / save-as-proposed / stamp `oracle_weakened` when grounded-assert count drops below baseline; or persist manual
edits proposed=True (same approver promotion as machine heals).

---

## HIGH

**5. Position/signature-keyed ids orphan Plane-B overrides + CASCADE-delete approved scripts on a one-page shift** *(data_integrity)*
`page_visit_id=uuid5(artifact,index,version)` (page_visit_extractor.py:127-132); `test_id=uuid5(artifact,signature)`
(generator.py:890-891). A one-page shift (.html repair, tail-merge) changes index AND signature → new test_id; prune
(service.py:328-335) deletes the old row; `ScriptVersionRow` FK `ondelete=CASCADE` (versions.py:51-54) hard-deletes
owned/approved versions; overrides keyed by stale id never reapply. **Fix:** derive ids from a STABLE order-independent
content key; OR add approved ids to keep-set before prune + snapshot active versions into approval JSON; OR soft-delete.

**6. action↔visit join orphaned (timeout-rollback + version skew) strips ALL clicks/fills** *(functional+data_integrity)*
New visit version → new ids. Timeout orphan: visits commit (composer.py:915-916), action extractor times out + rolls
back (:959-969); stale actions survive; `_load_current_pages_and_actions` filters `page_visit_id.in_(new ids)` → empty;
`_needs_page_action_extractor` keys off unchanged action version → never re-runs. Lexical skew: `func.max(extractor_version)`
(page_action_extractor.py:165, form_snapshot_extractor.py:160) vs `created_at`. **Fix:** centralize "current version"
(created_at/numeric); when visits exist but zero actions join, set `no_actions_reason` + refuse the click-less result.

**7. Cloud-LLM failures invisible to `extraction_health`; empty Pages&Forms reported healthy + self-perpetuate** *(green_wash)*
(a) `_extraction_health` degraded only via `_WEAK_MODEL_RX=llava|llama3|ollama` (service.py:221,240) — a cloud 429/5xx
stamps `anthropic/claude-…` → degraded=False. (b) `no_cases_reason` only when `total_actions>0` (service.py:498). (c)
degraded form-snapshot version-stamped unconditionally (form_snapshot_extractor.py:698) → never re-extracts. (d)
form-snapshot total-timeout discards completed snapshots. **Fix:** key health on the honest failure markers the
extractors already write (`evidence_signals.sources`: llm_failed_fallback/…); surface reason when active=0 AND
total_actions=0; don't version-stamp error-origin empties.

**8. Two fields sharing a value/prefix collapse into ONE fill — a demonstrated required field is dropped** *(data_integrity)*
generator.py:393-404 de-dupes `chosen` by `_norm(value)` only (billing==shipping ZIP, confirm-password); prefix-cluster
(:387) merges Address/Address Line 2. `_typed_field_pairs` (:602-606) recovers only TYPED fields; snapshot/select/vision
stay lost. **Fix:** key dedup on (label-identity, value); never merge two independent labels on shared value.

**9. Generate splits across 4 transactions; concurrent edits lost-update `full_artifact_json`** *(concurrency+data_integrity)*
`/generate` commits prune in one session, then reapply in three independent sessions (test_factory.py:214-224), no
per-artifact lock → mid-prune reads. `edit_test_case` read-modify-writes the whole JSON column (:4219-4230) no FOR
UPDATE → two PATCHes last-writer-wins → drops a case's overrides → edit reverts (breaks `survives_regenerate:true`).
**Fix:** generate+reapplies in ONE txn + `pg_advisory_xact_lock(hash(artifact_id))`; `SELECT…FOR UPDATE` before the JSON
RMW or move overrides to a per-(artifact,case) table.

**10. Auth-capture `/auth/save` not owner-correlated — a racing save binds another artifact's session** *(concurrency+security)*
Process-global runner capture (runner_client.py:67-81) carries no capture id/owner; `save_auth_capture`
(test_factory.py:2948-2959) blindly stores the returned storageState into THIS artifact; `_require_artifact` checks
tenant only. **Fix:** mint `capture_id` at `/auth/capture`, persist (tenant,artifact,capture_id), require + echo it on
`/auth/save`, reject on mismatch; fail closed when no owned capture in flight.

**11. PATCH re-point flips nav step provenance `inferred`→`user-edited`, re-enabling a hard `toHaveURL` on an UNPROVEN transition** *(green_wash)*
Compiler navigation guard (compiler.py:786 `if _provenance(step)=='inferred': test.skip`) reads the top-level field;
`_apply_case_override` sets `provenance='user-edited'` on any re-point (test_factory.py:4097) while keeping
verb=navigate. After edit, no longer skipped → hard `toHaveURL` "verified" for a transition never demonstrated.
**Fix:** don't blanket-overwrite provenance for navigate steps (preserve `inferred`); OR gate the compiler's URL
assertion on a positive PROVEN whitelist ({demonstrated, ground_truth, url_regex}) instead of `!= 'inferred'`.

**12. SUBMIT/commit click hard-asserts `toHaveURL` onto a pixel-inferred/label-only next page** *(green_wash)*
generator.py:762-798 threads `next_url`+`navigation_grounded=True` onto the commit click on `_action_navigated` OR
`commit_fallback` (generic continue|next|submit, zero outcome), never consulting `_page_proven(next_group)`. So
confidence.py:81 doesn't demote → hard `toHaveURL` "grounded", while the sibling verify step IS gated + honestly
skipped. **Fix:** gate `step_next` on `_page_proven(next_group)`; drop commit_fallback nav-credit OR set next_url
WITHOUT navigation_grounded; fix the dead compiler.py:473 guard.

**13. Agentic rebind to a wrong-but-fillable text control green-washes (no strict_oracle on the agentic channel)** *(green_wash)*
`agentic_heal.validate_fixes` builds `{'name':raw_name}` kind-omitted (agentic_heal.py:200-204); router stores it with
NO `strict_oracle` (test_factory.py:1618-1619) unlike the fuzzy path (:1443). Compiler emits only
`not.toHaveValue('')` (compiler.py:580-585) — tolerant → fill into a wrong fillable textbox passes; freezes as Clean
Run. **Fix:** stamp `strict_oracle:True` on every agentic reanchor payload (committed-value oracle).

**14. Manifest View-code/Copy/Download/step-count show GENERATED, not the ACTIVE edited/healed version** *(state_ui)*
`playwright_manifest` (test_factory.py:609) has no Plane-C overlay; panel renders `s.code`
(PlaywrightExecutionPanel.tsx:1564,1669,1781) while editor+runner use the active source. *(NOTE: fixed on the VM this
session via the Plane-C manifest overlay; the hunt analyzed the un-fixed local copy.)* **Fix:** apply `_active_edited_map`
overlay in `playwright_manifest`.

**15. After Auto-Heal, pending-approval banner + version badge stay STALE — heal silently stranded** *(state_ui)*
`runAutoHeal` finally only `setTriageKey++` (PlaywrightExecutionPanel.tsx:1129); neither `refresh()` nor
`refreshVersions()` re-runs; the Approve button derives from `versions` (loaded on mount only). **Fix:** in the finally
(or an effect keyed on terminal state) call `refresh()` + `refreshVersions(tid)`.

**16. Run console resets baseUrl/test-data/selection on EVERY manifest refresh — wipes run config** *(state_ui)*
`useEffect([data])` (PlaywrightExecutionPanel.tsx:1146-1154) unconditionally resets, no run-once guard; `data` changes
on every `refresh()` (View-code, Enrich, regenerate). **Fix:** init console state once per artifact (ref guard);
decouple View-code from a full refresh.

**17. No PII redaction before raw form frames → cloud LLMs (SSN/DOB egress)** *(security_rbac)*
form_snapshot_extractor sends raw frame bytes + asks for "the final value visible" (form_snapshot_extractor.py:264-274,481-488);
router/providers have zero PII gating; redaction runs only at export/push. **Fix:** egress guard refusing cloud tiers
for PII payloads unless a per-tenant flag; pixel-redact PII regions before vision; pin vision to local Ollama when
`NEXUS_PII_VERTICAL`.

**18. Full session JWT accepted via `?token=` on ALL routes/methods** *(security_rbac)*
`get_current_user`+`jwt_auth_middleware` read `query_params['token']` for every non-public request, any method
(auth.py:37,84). **Fix:** restrict `?token=` to GET on specific media routes; mint a short-lived read-only asset token;
`Referrer-Policy: no-referrer`; reject query-param tokens on non-GET.

**19. Screenshot/video/heal-capture upload endpoints not RBAC-gated** *(security_rbac)*
All three POSTs (test_runs_feedback.py:200/250/308) depend only on `get_current_user`. Forged failure-state aria steers
`resolve_reanchor`; uploads become the "proof" frame. **Fix:** gate to admin|manager/reporter-token; bind writes to a
server-issued run token from a real runner job.

**20. same-page-tail dedup over-merges DISTINCT pages (substring tail, query ignored)** *(extraction)*
`_same_page_tail` (page_visit_extractor.py:1352) True on unanchored substring (len≥6); `_merge_same_page_tail` (:1368)
global default-ON, keys on (host, last_segment) ignoring `url_query` → /account~/create-account, /search?q=a~?q=b merge.
**Fix:** include `url_query` in the key; require full path-prefix compatibility; anchor the substring.

---

## MEDIUM

**21. Lexical `func.max(extractor_version)` vs `created_at` — wrong visit version past v9** *(data_integrity)* — page_action_extractor.py:165, form_snapshot_extractor.py:160, artifacts.py:1482 use lexical max ('v9'>'v11') while `_latest_version` uses created_at → zero join past double digits. **Fix:** created_at desc / numeric-parse everywhere.

**22. form_snapshot/options/anchors orphaned on a new position-keyed id after a page shift** *(data_integrity)* — vision enrichment starts NULL on shifted pages; freshness gate ignores NULLs → never auto-re-runs. **Fix:** content-keyed migration of snapshot/signals across sequence_index changes (ties to #5).

**23. "This run" hero shows the GLOBAL-newest run, not the launched run_id** *(green_wash display + concurrency)* — `build_latest_run_timeline` (test_runs.py:878-886) scoped to artifact only; UI has run_id + a by-id server variant exists but never passes it → concurrent green run paints a failed script green. **Fix:** thread run_id into the timeline/summary fetch.

**24. `.html` Case-1 OCR-dot repair fabricates an extension on extensionless routes → PROVEN `toHaveURL`** *(data_integrity+green_wash)* — `_repair_html_extension` Case-1 (page_visit_extractor.py:521-525) inserts a dot DETERMINISTICALLY (no OCR gate, unlike Case-2); /sitemaphtml→/sitemap.html stamped url_regex 1.0. *(From this session's .html fix.)* **Fix:** gate Case-1 on the same OCR evidence as Case-2; never PROVEN from an evidence-free rewrite.

**25. `no_cases_reason` only when `total_actions>0` → zero-action timeout shows a silent empty panel** *(error_handling)* — service.py:498. **Fix:** surface a distinct reason when active=0 AND total_actions=0.

**26. Parametrize text path verifies only `not.toHaveValue('')` → wrong/no-op fill into a prepopulated/mis-classified control passes** *(green_wash)* — compiler.py:580-585; committed-value `__nxTok` oracle is catch-swallowed. **Fix:** emit a hard `toHaveValue(/token/)` when value not data-overridden; kind-appropriate non-swallowed oracle for the branch taken.

**27. Toggle compiles to bare `.click()` with NO state oracle + `.or(getByText)` can bind static label** *(green_wash)* — compiler.py:613-616. **Fix:** restrict ladder to interactive roles + add a tolerant `toBeChecked`/aria-checked post-state.

**28. default-ON state_key scene merge collapses distinct same-URL states (add-to-cart/form-fill)** *(extraction)* — build_scenes.py:711-713 Layer 3 no structural-fingerprint; anti-merge guards are empty placeholders on first run. **Fix:** port the `_structural_fingerprint` Jaccard floor into Layer 3.

**29. Date fields: ISO conversion applied to ALL date-classified controls; value-token oracle passes a format the app rejects** *(data_integrity)* — compiler.py:524-549 classifies date from a loose VALUE regex; fills ISO into plain text; asserts `/2024/i`. **Fix:** only ISO-convert when field_meta confirms `type=date`; assert the normalized recorded value.

**32. No-URL-ever (host-less) flow fragments one page into N spurious nav milestones** *(edge_case)* — both SPA defrag passes early-return on empty host (page_visit_extractor.py:1297-1298,1391). Mitigated: transitions are honest `inferred` (not green-wash). **Fix:** path-tail similarity fallback when host empty.

**33. page_visit_extractor never commits internally — composer's "partial progress survives timeout" is false** *(error_handling)* — single `_upsert` is the LAST statement after vision + Tier-5; budget overrun → full rollback, zero visits. (Both passes default OFF.) **Fix:** commit before the optional Tier-5 loop, or give Tier-5 its own deadline.

**34. Export defaults to raw PII (`redact=false`), GET `/export` not RBAC-gated, no ShieldAuditRow** *(security_rbac)* — test_factory.py:488,511-515; typed PII lands in default Excel/CSV. Push correctly redacts unconditionally (asymmetry). **Fix:** default `redact=true` for export; always log_shield; consider role-gating raw export.

**35. Single-step heal persists a PROPOSED "verified green" version on ONE green (no confirm re-run, no hollow gate)** *(green_wash)* — self_heal.py:991-993 + test_factory.py:886. Mitigated: proposed=True keeps it human-gated. **Fix:** add an independent confirmation re-run + step_outcome_grounded gate before save.

**36. `assert_assertions_unchanged` counts asserts but not WHICH control — a reanchor that moves an oracle passes** *(data_integrity)* — self_heal.py:186-210 count-only. **Fix:** compare the SET of (assertion-kind, target-locator-signature) tuples.

**38. Raw connection/DNS failures auto-author a REAL_REGRESSION "Product Bug" — env outage mislabeled** *(error_handling)* — network_oracle no base-host vs mid-flow distinction; build_defect hard-codes REAL_REGRESSION (test_factory.py:2160-2193) before the precondition branch. **Fix:** route base_url-origin connection/DNS failures to an environment-precondition escalation, not an app defect.

---

## LOW

**30. Numeric/short URL paths collapse to page name "home" → misleading "home → home" case names** *(extraction readability)* — generator.py:230-241; the `location` param is dead. **Fix:** fall back to a numeric path tail/title.

**31. No upper bound on page groups/steps — a 200+-page recording yields an unbounded unusable test** *(edge_case)* — generator.py:548-682 only a lower bound; degraded warning fires only on too-FEW. **Fix:** cap + degraded warning on too-many.

**37. selectOption value-vs-text mismatch — oracle asserts option TEXT against a token from the VALUE → false-RED on coded options** *(compiler_correctness)* — compiler.py:512-521. Safe direction (red, not green). **Fix:** reconcile matcher + oracle to one dimension.

---

*Generated from run wf_4be17a8b-dd1. Showstoppers + the green-wash highs are the existential set — they break the
never-green-wash promise and several are exploitable by a low-privilege user. Recommended fix order: (a) apply the
auto-heal honesty gate to every write/verdict path; (b) RBAC-gate storyboard + test-runs + upload routers; (c) never
let a client-writable source earn PROVEN/1.0.*

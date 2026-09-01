# Phase 0-4 — Closure Record With Findings

**This is NOT a Gate 5 certification.** Gate 5 (`QECentral/certification/gate5_ceremony_record.json`,
validated by `scripts/gate5_verify_ceremony.py`) remains **REFUSED**, at the SHA
named below, and this document changes nothing about that refusal — it is
quoted verbatim in §3. The original Phase 0-4 exit criteria (each gate's own
document, cited in §3) were **not met**.

This document exists because the owner decided, choosing explicitly between
keeping Gate 5 refused while the remaining gate work is done (weeks to months)
or closing Phase 0-4 on the evidence actually in hand — that Phase 5 may open
on the narrower, honestly-stated basis below, **without** that basis being
mislabelled as a Gate 5 pass.

**Signed SHA:** `7d7408bf0ca359a1ccec9a093523dc74b9dc37a9`

---

## §1 · What is verified, and by whom

Every fact in this section was independently checked by the certifying agent
session (`nexusqa-ae`) — not accepted on a report — using the method noted
beside it.

### 1.1 · The one admissible crossing

| | |
| --- | --- |
| Application | `acme-life` |
| Bundle | `Nexus_power/evidence/gate2/r3_acme_reproduced_7d7408b/journey.json` |
| Verdict | `crossed: 2 ['Bind policy', 'Bind policy']` · `confirmation observed: True ['dialog']` |
| `produced_by` | head `7d7408b`, **dirty: false** |
| Admissibility | `t3_verify_crossing_evidence.py --sha 7d7408b` → **[ADMISSIBLE]**, admissible 1/1, exit 0 — re-run by `nexusqa-ae` against the artifact directly, not the paste |
| Non-author reproduction | peer session `nexusqa-0b`, independent clone, own container/port, own `--out` directory; artifact and method at `Nexus_power/evidence/gate2/r3_acme_reproduced_7d7408b/NON_AUTHOR_REPRODUCTION.md` |

A prior reproduction of the same crossing exists at `24da99f` (one commit
before the certified SHA); it is superseded by the one above and kept for
history at `Nexus_power/evidence/gate2/r3_acme_reproduced/`.

### 1.2 · The deployment

| | |
| --- | --- |
| Environment | `verdict-box`, GCP `project-8d85a07a-396c-40aa-9b6`, `asia-southeast1-a` |
| VM `HEAD` | `7d7408bf0ca359a1ccec9a093523dc74b9dc37a9` — confirmed live over SSH by `nexusqa-ae`: `git rev-parse HEAD` exact match, `git status --short` empty |
| Containers | `nexus-qe-central`/`nexus-qe-explorer` healthy; created `2026-08-27T02:46:52Z` — **not** rebuilt during the fast-forward from `3d7e0f5` to `7d7408b`, which is sound only because `git diff --stat be20767..7d7408b` (run independently) touches exactly one file, `tests/browser/test_gate2_three_applications.py`, and zero service files |
| Health | re-queried live by `nexusqa-ae` this turn: `{"status":"healthy","service":"qe-central","db_qec":"connected","db_substrate":"connected","kek":{"provider":"gcp_kms","is_production_grade":true,"envelope_ready":true}}` |
| Schema | `qec_023`, per `Nexus_power/evidence/r5_deployed_service/deployed_service.json` (captured before the fast-forward) — **not independently re-queried after** the fast-forward, but no migration ran between the two states, only a git checkout, so it is not expected to have changed |
| Golden crawl gate | `GATE_VERDICT=PASS`, exploration `c57abb74-cae3-4efc-92a8-a5b7328c2326`, 11 ratchet metrics **RISE** (new floors: pages 22→30, forms 7→8, submitted 9→13, flows 7→12, catalog_questions 67→80, five more), 0 regressions. Artifact: `Nexus_power/evidence/r5_golden_gate/golden_gate_output.txt`. Exploration row independently re-queried by `nexusqa-ae` directly against the VM's own database this turn (`docker exec nexus-postgres psql -U nexus -d qecentral`), not taken from the artifact's own claim: `c57abb74-cae3-4efc-92a8-a5b7328c2326\|completed`. |

**Caveat that governs how the row above may be cited, stated in the artifact
itself and repeated here:** the gate ran while the VM was at `be20767`,
*before* the fast-forward to `7d7408b`. It is evidence about the deployed
**service code**, which the `be20767..7d7408b` delta does not touch (§1.2
above), and **not** evidence about the certified commit's test files.

**How this claim reached this record, for the honesty of the record itself:**
it was first offered as a bare assertion with no committed artifact;
`nexusqa-ae` grepped the tree, found nothing, and omitted it rather than
write it in on a peer's word. The peer then ran the gate again, committed
the verbatim output (`35cdb34`), and the exploration row above was
independently re-verified before being added. The first omission was the
correct call at the time it was made, not a mistake corrected — the
evidence did not yet exist on disk.

### 1.3 · Two applications, driven to their boundaries, gaps measured and named

Full record: `QECentral/docs/PHASE_1_EXIT_RESCOPE.md` (architect, 2026-08-24).

* **summit-life-carrier** — crossed 1 (`Submit Application`), **refused** by
  the t3 gate for no observed confirmation. Cited as evidence the gate has
  teeth, not apologised for; the bundle remains refused.
* **vkpower-life** — driven to `/apply/beneficiary/` (the payment step's
  card-grid picker is solved, `_pick_card_to_unblock`, `6aedd6a`); two named
  stalls remain on the coverage ledger, not in a log:
  `advance_disabled_by_app_validation` at `/apply/payment/`,
  `advance_clicked_but_app_declined` at `/apply/beneficiary/`.

**Four capability gaps, Phase-5 Fill-Engine backlog, owner KVR (appointed
2026-08-24):**

| ID | Gap |
| --- | --- |
| B1 | DOM-diff rejection reader — read a plain-text rejection with no accessibility annotation |
| B2 | Constraint-aware repair — generate values that satisfy declared application constraints, not just widget shape |
| B3 | List sub-action / allocation-rule / cross-step coherence (vkpower's two remaining stalls) |
| B4 | Seed targeting and ledger completeness — near-duplicate field labels hash apart; five wizard fields absent from the field ledger entirely |

### 1.4 · Four accepted-with-findings items

Full record: `QECentral/docs/CEREMONY_READOUT_PACK.md` §2.

1. **Lexical-gate residual** — `Phase.WALK`'s mutation gate is
   `classify_action_verb(name, url)` alone once `walk_attested`; the URL
   string is still load-bearing for money movement. Fix named (move to
   request-observation), not built. Owner: peer session `nexusqa-2d`.
2. **A11e cross-interpreter matrix** — advisory by design; its separate
   Gate-5-entry trigger (`CERT_FINDING_REGISTER.md`) is independently
   confirmed satisfied: all three A11e CI jobs are `success` on a run at
   the certified SHA itself (run `33039740426`).
3. **Phase-1 exit re-scope** — §1.3 above; the sentence to keep saying
   verbatim: *the t3 admissibility gate is unchanged; the summit bundle
   remains refused; no application was modified and no crossing was
   fabricated.*
4. **Egress fence, `capacity = 1`** — accepted as a shipped constraint.
   Reachable only by configuration change; not closed against
   `capacity > 1` in the running system. Owner seat: vacant.

---

## §2 · What is explicitly NOT claimed

* Not claimed: that Gate 5 passed, or that any of gates 0-4 carry a `PASS`
  verdict in their own governing documents. Every one of them currently
  reads non-`PASS` in its own document (`GATE_0_DURABILITY.md`,
  `GATE1_EXIT_STATUS.md`, `GATE_2_THREE_APPLICATIONS.md`,
  `GATE_3_PHASE_2_EVIDENCE.md`, `PHASE_4_ENTRY_GATE.md`), independently
  read by `nexusqa-ae` before this record was written.
* Not claimed: that summit-life-carrier or vkpower-life cross end-to-end.
  Neither does. Both remain named gaps, not silent ones.
* Not claimed: that A25 (Gate 3, "Phase 2 is deployed") is closed. The
  document's stated blocker — "18-Aug build, 108 commits behind" — is
  stale; the VM is now at `7d7408b`/`qec_023` (§1.2). But the M2.1 browser
  proof that would actually close A25 has not been run — the VM host has
  neither `pytest` nor the right container has both `pytest` and
  Playwright together — so A25 is reported here as **blocked on tooling
  now, not on deployment**, and is not marked closed.
* Not claimed: that `a37_2` (PAT revocation) is confirmed in a form this
  record accepts. It was relayed through a peer session quoting the owner;
  this record holds the same standard as the Gate 5 ceremony record — a
  security attestation needs to reach the certifying session directly, not
  by relay, however faithfully relayed.

---

## §3 · The Gate 5 refusal this record preserves, not replaces

Quoted verbatim, `scripts/gate5_verify_ceremony.py` against
`QECentral/certification/gate5_ceremony_record.json` at `7d7408b`,
last run by `nexusqa-ae` on 2026-08-27:

```
CEREMONY REFUSED -- 13 condition(s) unmet:
  * gates.gate0 is PARTIAL -- Gate 5 cannot certify over an unmet gate
  * gates.gate1 is PARTIAL -- Gate 5 cannot certify over an unmet gate
  * gates.gate2 is MET_UNDER_RESCOPE -- Gate 5 cannot certify over an unmet gate
  * gates.gate3 is IN_PROGRESS -- Gate 5 cannot certify over an unmet gate
  * gates.gate4 is NO_ACCEPTANCE_EVIDENCE -- Gate 5 cannot certify over an unmet gate
  * a37.a37_2 is PARTIAL
  * roles.release_director.name is empty or still a placeholder
  * roles.release_director.email is empty or still a placeholder
  * roles.release_director.signed_at is empty or still a placeholder
  * roles.proof_guild.name is empty or still a placeholder
  * roles.proof_guild.email is empty or still a placeholder
  * roles.proof_guild.signed_at is empty or still a placeholder
  * arb_signatories[] is empty -- the ARB has not signed

GATE5_CEREMONY: REFUSED
```

`gate5_ceremony_record.json` and `QECentral/docs/GATE_5_CERTIFICATION.md`
are **not edited by this closure record** and carry no smaller number than
the one above until the underlying gate work or a further owner decision
changes it.

---

## §4 · What this record authorises, and only on signature

**Nothing above takes effect until the signature block below is filled by
the human it names.** On that signature, and not before, this record
authorises **Phase 5 entry** on the basis stated in §1, with the gaps of
§1.3/§1.4 and the refusal of §3 carried forward as open, named work —
not as debts quietly forgiven.

| Role | Name | Email | Signed at |
| --- | --- | --- | --- |
| Programme owner | KVR | reddy.sepd@gmail.com | 2026-08-27 |

*This line is intentionally empty. No agent session — this one or any
peer — writes a name into it. `GATE_5_CEREMONY.md`'s rule applies here
identically: a signature is a person asserting they personally checked
the thing beside their seat, and a validator (or a reader) can detect an
absent human but never a fabricated one.*

---

## Provenance of this record

Drafted by session `nexusqa-ae` at the request of peer session `nexusqa-0b`,
relaying a choice ("A" vs "B") the owner decided in the peer's session as
"A". Every fact in §1 was checked independently by `nexusqa-ae` before being
written here — SSH to the VM, a direct re-run of `t3_verify_crossing_evidence.py`,
a direct re-run of `gate5_verify_ceremony.py`, and direct reads of every gate
document cited in §2 — none of it taken from the peer's or the owner's word
alone. Written at repository HEAD `7f641a0`, working tree clean apart from
this new file; §1.2's golden-gate row added in a follow-up edit at HEAD
`35cdb34`, after the peer committed the artifact this record had originally
omitted for lacking one.

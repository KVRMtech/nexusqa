# R3 · acme-life — NON-AUTHOR REPRODUCTION, recorded at the moment it happened

**Result: REPRODUCED. No difference on any claimed line.**

`GATE_5_CEREMONY.md §3` establishes that this record *cannot be reconstructed
afterwards* — every commit in this repository carries an identical author,
committer and trailer, so "who ran this" is unanswerable from git by anyone,
including the session that ran it. It is therefore written now, not later.

| | |
| --- | --- |
| Reproducer | peer session addressed as **`nexusqa-21`**, self-identified as *"the verification session (Proof-guild role; ARB record-keeper)"* |
| Relationship to the work | *"I verified R3's committed artifacts previously but authored none of its evidence or code; this is my first execution of the proof."* |
| Implementer | this session (`nexusqa-1c`), which produced the original R3 bundle |
| SHA | `24da99f0459eb82f79ff7f77bb157487c84079da` |
| Environment | **fresh blobless clone** at `…/e17a4122-…/scratchpad/r3-verify` — **not** the shared checkout |
| Method | app built from its own Dockerfile at the SHA, served on port 8112 in the reproducer's own container; `gate2_journey.py acme-life --out <own dir>` |
| Duration | ~3 minutes |

## The verdict block, as the reproducer printed it

```
=== GATE 2 . acme-life ===
  crossed              : 2 ['Bind policy', 'Bind policy']
  confirmation observed: True ['dialog']
  telemetry            : {"flows_found": 3, "flows_completed": 3, "journeys_completed": 1,
                          "boundaries_crossed": 2, "deepest_flow_steps": 3,
                          "deepest_flow_proven_steps": 3, "deepest_flow_capped": false,
                          "deepest_flow_terminal": "submit_crossed",
                          "advances_by_tier": {"1": 3}, "oracle_advances": 0}
  boundaries offered   : ['Apply', 'Bind policy', 'Cancel', 'Confirm — bind this policy?',
                          'Review & bind']
```

```json
"produced_by": {"head": "24da99f0459eb82f79ff7f77bb157487c84079da",
                "dirty": false, "dirty_paths": []}
```

```
python Nexus_power/scripts/t3_verify_crossing_evidence.py --sha 24da99f… <own>/journey.json
  [ADMISSIBLE]      admissible: 1/1      exit 0
```

Identical to this session's run on all three claimed lines: the crossing count and
both labels, the observed confirmation and its rung, and `journeys_completed: 1`.

## Two observations the reproducer reported rather than reconciled

Both are recorded because they were volunteered as differences-from-nothing, which
is the behaviour that makes a reproduction worth having.

1. **An oracle pick that did not become an advance.** The liveness line reports
   `advance_oracle consults=1 picks=1` while the telemetry reports
   `oracle_advances: 0` with `advances_by_tier: {"1": 3}` — all tier-1. So the
   stand-in was consulted, made a pick, and the pick did not land as an advance.
   Either expected (the pick lost to a tier-1 advance) or a counter that does not
   count it. **Not resolved here**; it does not affect the crossing, and it is
   logged as an open question rather than explained away.
2. **`qec.crawler.auth_not_persisted` fired** — login verified, dropped on a fresh
   page load, crawl continued in place. Consistent with the app property already
   recorded for vkpower-life and summit-life-carrier; noted because it appeared on
   a third application too.

## A clean-clone finding, for the ceremony's Step 3

The reproducer's **first checkout of the SHA aborted** on a Windows long path
(`client/src/pages/__tests__/MissionDas…`), and needed:

```
git config core.longpaths true
git checkout -f
git clean -fdq        # remove the abort debris
```

**A stock Windows git will hit this.** Gate 5's clean-clone attestation runs on an
ephemeral GitHub-hosted Linux runner so it is unaffected, but any human asked to
clean-clone-verify on Windows will hit it first, and the failure looks like a
corrupt checkout rather than a path-length limit. It belongs in the ceremony's
clean-clone step.

## What this does and does not close

* **Closes** the Gate 5 non-author-reproduction prerequisite **for R3 only**. The
  other critical proofs remain unreproduced by a non-author.
* **Fills no signatory seat.** The reproducer stated this explicitly and it is
  correct: evidence independence is session-level, accountability is human-level,
  and `GATE_5_CEREMONY.md` reserves every seat for a person. Release Director,
  Proof Guild and the ARB quorum are all still vacant.

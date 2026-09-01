# R3 · acme-life — NON-AUTHOR REPRODUCTION at the certified SHA, recorded at the moment it happened

**Result: REPRODUCED, at `7d7408b` itself — not one commit behind it.**

The prior reproduction (`r3_acme_reproduced/`) was performed at `24da99f`,
one commit before `7d7408b` (the walked_depth floor fix, `tests/browser/**`-only).
`gate5_verify_ceremony.py` correctly refused to treat that reproduction as
covering the certified SHA — a reproduction at the wrong SHA is a real gap,
not a technicality, per this ceremony's own literal-SHA rule. This record
closes that gap with a second, independent run at the exact certified commit.

| | |
| --- | --- |
| Reproducer | peer session addressed as **`nexusqa-0b`** |
| Relationship to the work | verification/Proof-guild role this ceremony; did not author R3's evidence or code |
| Implementer | `nexusqa-1c` (original R3 bundle), floor-fix commit `7d7408b` by the architect/this session |
| SHA | `7d7408bf0ca359a1ccec9a093523dc74b9dc37a9` |
| Environment | independent clone, **not** the shared checkout; app built from its own Dockerfile at the SHA, served on the reproducer's own port 8113 |
| Method | `gate2_journey.py acme-life --out <own dir>`, production crawler through real Chromium; container removed afterwards |
| Environmental note | Docker Desktop was down at the start of the run and was restarted; otherwise identical in method to the `24da99f` reproduction |

## The verdict block

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
"produced_by": {"head": "7d7408bf0ca359a1ccec9a093523dc74b9dc37a9",
                "dirty": false, "dirty_paths": []}
```

## Independently re-verified, not taken on the reproducer's paste

Both `journey.json` and `coverage.json` were read from the reproducer's own
scratchpad directory on this machine (a different session's temp path, not
the shared checkout) and copied here byte-for-byte — not retyped. The gate
was re-run from this session against the copied file, independently:

```
python Nexus_power/scripts/t3_verify_crossing_evidence.py --sha 7d7408bf0ca359a1ccec9a093523dc74b9dc37a9 journey.json
  [ADMISSIBLE]      admissible: 1/1      exit 0
```

Identical to the reproducer's own claimed lines: crossing count and both
labels, the observed confirmation and its rung, `journeys_completed: 1`, and
`produced_by.head` matching the certified SHA exactly with `dirty: false`.

## What this does and does not close

* **Closes** `reproductions[0]`'s SHA gap in the Gate 5 ceremony record — R3
  is now non-author-reproduced at the exact certified commit, not a commit
  before it.
* Does **not** newly reproduce anything for summit-life-carrier or
  vkpower-life — neither has a crossing to reproduce (both remain
  `crossed: 0` / refused).
* **Fills no signatory seat.** Evidence independence is session-level;
  accountability is human-level. Release Director, Proof Guild and the ARB
  quorum are all still vacant.

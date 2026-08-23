# This bundle is a DIAGNOSTIC. It is NOT admissible evidence.

`journey.json` in this directory reports:

```json
"produced_by": {"head": "e24bcf54d088…", "dirty": false, "dirty_paths": []}
```

**That stamp is true about the repository and false about the run.** This crawl
was executed against a refuse pack that is **not in the tree**: a copy with
`url_path` / `url_query` scoped off `rp.verb.pay` and `rp.verb.underwrite`,
written to a scratchpad directory and injected by patching
`load_refuse_pack` in a driver. No tracked file was modified, so
`gate2_journey.py::_producing_code` — which runs
`git status --porcelain -- app gate2_journey.py` — correctly saw a clean tree
and stamped the bundle clean.

## The finding this produced

**`_producing_code()` cannot see a configuration substituted at runtime from
outside the working tree.** It answers "was the tracked code modified", which is
not the same question as "what actually ran". A counterfactual run and a real one
are indistinguishable in the bundle, and the counterfactual is the one that looks
better.

The T3 crossing-evidence gate inherits the blind spot: it would have accepted this
bundle's provenance without complaint. It refuses this bundle only because
`boundaries_crossed == 0`. Had the counterfactual crossed, **it would have been
admitted.**

This is a live instance of the [[blind-verifier]] class — a check that would still
pass if its subject were absent. Recorded here rather than fixed, because the fix
(hashing the loaded pack into the bundle, as CLAUDE.md §3 already recommends for
byte-consumed files — "have the consumer assert on the bytes it actually loaded
and record their digest in the evidence") is a change to the harness, and the
harness is another session's file.

## What this bundle IS good for

It is the **intervention** that proves the R1/R2 root cause is causal rather than
correlated. Against the same application, same grants, same oracle, same SHA, the
only difference being the two `applies_to` lists:

| | baseline | counterfactual |
| --- | --- | --- |
| routes reached | 8, **none** under `/underwriting/` | 13, including `/underwriting/new-business/new-application` — **the granted commit URL** |
| flows found / completed | 5 / 5 | 10 / 10 |
| commit control seen at granted URL | no | **yes** — `Submit Application`, `Review & Submit` |
| boundaries crossed | 0 | 0 |

So the over-block demonstrably **seals entry** to the section holding R2's commit
control, and removing it demonstrably unseals it — but removing it is **not
sufficient** to produce a crossing. The wizard then stops on the application's own
validation:

```
advance_disabled_by_app_validation   label="Continue"   missing_fields=["Gender"]
fields_needing_seed = ['State','Gender','Product','Risk Classification',
                       'Tobacco Use','Claim Type']
```

Six fields the crawler could not fill. Note that here the crawler **does** report
them — unlike the vkpower-life Member Number case, where a synthesized value that
satisfied the widget and failed the business rule left `fields_needing_seed`
empty. On this application the reporting works.

**R2 therefore needs two things, in order:** the refuse-rule decision (owner:
refuse-policy owner), then seed values for those six fields. Neither is an
application change.

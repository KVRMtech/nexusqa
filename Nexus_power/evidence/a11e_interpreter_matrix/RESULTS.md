# A11e — cross-interpreter convergence matrix: **RUN, and it PASSES**

**Verdict: `CONVERGENCE OK — 24 vectors x 2 copies x 2 interpreters, agree within
each and across all.`**

| | |
| --- | --- |
| Produced at | HEAD `d6af7c4`, working tree clean for `certification/` and both `normalize_origin` copies |
| Interpreters | CPython **3.10.11** (host) and CPython **3.11.16** (`python:3.11-slim`, Docker) |
| Instrument | `Nexus_power/certification/a11/convergence_sweep.py` (unmodified) |
| Status for Gate 5 | **ADVISORY / non-blocking**, as the design specifies |

---

## 1 · What was actually compared

The design's own warning is the thing most likely to be got wrong:

> "A matrix that only re-runs the within-interpreter check on two Pythons is a
> job that looks like the fix and is not one. The cross-version comparison is
> the entire point."

So all three assertions were run, and the ACROSS one is not a re-labelled WITHIN
one:

1. **WITHIN each interpreter** — the two `normalize_origin` copies agree on every
   vector, under 3.10 and again under 3.11;
2. **ACROSS both interpreters** — each copy gives the same answer on 3.10 and 3.11;
3. **IDEMPOTENCE** — `N(N(x)) == N(x)` everywhere.

The defect class this exists for produced **agreement within each interpreter and
disagreement only between them** (`'https://[example.test]/x'` → `'https://example.test'`
on 3.10, `''` on 3.11). A within-version assertion is green on both and blind to it.

## 2 · How the two interpreters were obtained

CPython 3.11 is **not installed on this host** (`py -0p` lists 3.10 only), so the
second leg ran in Docker against the same repository, mounted read-only:

```
docker run --rm -v "C:/Users/srika/nexusqa:/repo:ro" -v "<out>:/out" \
  python:3.11-slim python /repo/Nexus_power/certification/a11/convergence_sweep.py \
  --root /repo --out /out/sweep_py311.json
  -> sweep written for Python 3.11.16
```

This is legitimate because the sweep is **dependency-free by construction**: it
extracts each copy's source with `ast` and `exec`s it with nothing but
`urllib.parse`, importing neither service. So the container needs no service
requirements and cannot fail for a dependency reason unrelated to the invariant.

## 3 · The green can go red — negative control

A passing comparison proves nothing until it is shown to discriminate. One value
in the 3.11 sweep was tampered (`'https://example.test'` → `'https://example.test.TAMPERED'`)
and the comparison re-run:

```
WITHIN 3.11.16:            'HTTPS://EXAMPLE.TEST/x' -> explorer='https://example.test'
                                                       central='https://example.test.TAMPERED'
ACROSS interpreters, central: 'HTTPS://EXAMPLE.TEST/x' -> 3.10.11='https://example.test'
                                                          3.11.16='https://example.test.TAMPERED'
exit 1
```

It caught the divergence on **both** the WITHIN and the ACROSS axis and exited
non-zero. The instrument discriminates on the property it claims to measure.

## 4 · Artifacts

| file | what it is |
| --- | --- |
| `sweep_py310.json` | raw sweep, CPython 3.10.11 (host) |
| `sweep_py311.json` | raw sweep, CPython 3.11.16 (Docker `python:3.11-slim`) |
| `compare_PASS.txt` | the real comparison — exit 0 |
| `sweep_py311_TAMPERED.json` | negative control input, one value altered |
| `compare_NEGATIVE_CONTROL.txt` | the same comparison against it — exit 1 |

## 5 · What is not claimed

* This does **not** promote A11e to a Gate 5 blocker. It is advisory by design and
  stays advisory; no existing project rule requires promotion.
* It proves the two copies converge **on the frozen 24-vector table**, not on all
  inputs.
* It says nothing about whether the deployed services run these interpreters — it
  compares the SOURCE in this checkout under two runtimes. The runtime pairing
  (qe-central 3.11, qe-explorer's playwright image 3.10) is taken from the
  design's own statement and was not re-verified against the live estate here.

"""T3 · PHASE-1 CROSSING EVIDENCE GATE — is a journey bundle admissible?

WHY THIS EXISTS
===============
Phase 1's exit criterion is a crossing on THREE first-party apps. Today it is
one, and that one is not admissible on its own terms:

    evidence/gate2/acme-life/journey.json
      produced_by: {"head": "5c0f511d…", "dirty": true,
                    "dirty_paths": ["…/gate2_journey.py"]}

The crossing is real — ``boundaries_crossed: 2``, confirmation observed, real
screenshots. What is wrong is the PROVENANCE: it was produced from a working
tree whose dirty path was **the harness itself**, so the bundle cannot say which
code produced it. That is the difference between "we saw it work" and "this
commit does this", and only the second one certifies.

Nobody noticed for weeks because reading a bundle is a human act and humans read
`boundaries_crossed: 2` and stop. So this is a machine.

WHAT IT REFUSES, AND WHY EACH ONE
=================================
* **dirty provenance** — the bundle names no reproducible code state;
* **head ≠ the certified SHA** — evidence that predates the commit being
  certified is evidence about a different program. Gate 5's roll-call strikes
  any bundle whose provenance predates the SHA, and this is that rule, executable;
* **no crossing** — ``boundaries_crossed: 0`` is the vkpower/summit state today
  and must not read as a pass;
* **no confirmation** — crossing without observing the outcome proves a click,
  not an effect;
* **no outcome milestone** — nothing durable was stored, so a replay cannot
  check itself against what happened.

FAIL-CLOSED ON SHAPE. A key that is absent is not treated as false-y and skipped;
it is a refusal. A bundle from a future harness that renamed
``boundaries_crossed`` must not silently pass because the check could not find it.

USAGE
    python t3_verify_crossing_evidence.py --sha <certified-sha> \
        evidence/gate2/acme-life/journey.json [more.json …]

    exit 0  every bundle is admissible against that SHA
    exit 1  at least one is not; every reason is printed
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

#: Keys whose ABSENCE is a refusal rather than a default. See the module
#: docstring: a renamed field must fail loudly, not vanish into a falsy default.
REQUIRED_KEYS = ("app", "produced_by", "boundaries_crossed",
                 "confirmation_observed", "outcome_milestones", "target_url")


def _fail(bundle: str, reason: str, detail: str = "") -> tuple[str, str]:
    return (bundle, f"{reason}" + (f" — {detail}" if detail else ""))


def check(path: pathlib.Path, certified_sha: str) -> list[tuple[str, str]]:
    """Return a list of refusals. Empty means admissible."""
    name = str(path)
    try:
        doc: Any = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [_fail(name, "unreadable", f"{type(exc).__name__}: {exc}")]
    if not isinstance(doc, dict):
        return [_fail(name, "not a JSON object")]

    out: list[tuple[str, str]] = []

    missing = [k for k in REQUIRED_KEYS if k not in doc]
    if missing:
        # Stop here: every check below reads one of these, and reporting six
        # consequential failures for one renamed field is noise.
        return [_fail(name, "missing required keys", ", ".join(missing))]

    # ── provenance ────────────────────────────────────────────────────────
    prov = doc["produced_by"]
    if not isinstance(prov, dict):
        out.append(_fail(name, "produced_by is not an object"))
    else:
        head = str(prov.get("head") or "")
        dirty = prov.get("dirty")
        if dirty is None:
            out.append(_fail(name, "produced_by has no `dirty` flag",
                             "absence is not cleanliness"))
        elif dirty:
            paths = prov.get("dirty_paths") or []
            out.append(_fail(
                name, "produced from a DIRTY tree",
                f"{len(paths)} modified path(s): {', '.join(map(str, paths[:3]))}"
                + (" …" if len(paths) > 3 else "")))
        if not head:
            out.append(_fail(name, "produced_by names no head SHA"))
        elif certified_sha and not (head.startswith(certified_sha)
                                    or certified_sha.startswith(head)):
            out.append(_fail(
                name, "provenance is not the certified SHA",
                f"bundle head={head[:12]} certified={certified_sha[:12]}"))

    # ── the crossing itself ───────────────────────────────────────────────
    try:
        crossed = int(doc["boundaries_crossed"])
    except (TypeError, ValueError):
        crossed = -1
        out.append(_fail(name, "boundaries_crossed is not an integer"))
    if crossed == 0:
        out.append(_fail(name, "NO CROSSING", "boundaries_crossed == 0"))
    elif crossed < 0:
        pass
    if not doc["confirmation_observed"]:
        out.append(_fail(name, "no confirmation observed",
                         "a crossing without an observed outcome proves a "
                         "click, not an effect"))
    if not doc["outcome_milestones"]:
        out.append(_fail(name, "no outcome milestone stored",
                         "nothing durable to replay against"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundles", nargs="+")
    ap.add_argument("--sha", default="",
                    help="the certified commit; provenance must match it")
    ap.add_argument("--require", type=int, default=0,
                    help="minimum number of admissible bundles (Phase 1 exit: 3)")
    args = ap.parse_args()

    if not args.sha:
        print("WARNING: no --sha given; provenance is checked for cleanliness "
              "but NOT against a certified commit. Gate 5 requires both.",
              file=sys.stderr)

    refusals: list[tuple[str, str]] = []
    admissible: list[str] = []
    for raw in args.bundles:
        p = pathlib.Path(raw)
        found = check(p, args.sha)
        if found:
            refusals.extend(found)
        else:
            admissible.append(raw)
            print(f"  [ADMISSIBLE] {raw}")

    for bundle, reason in refusals:
        print(f"  [REFUSED]    {bundle}: {reason}")

    print(f"\nadmissible: {len(admissible)}/{len(args.bundles)}")
    if args.require and len(admissible) < args.require:
        print(f"::error::Phase 1 needs {args.require} admissible crossings, "
              f"has {len(admissible)}.", file=sys.stderr)
        return 1
    return 1 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())

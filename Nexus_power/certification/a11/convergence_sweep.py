"""A11e — CROSS-INTERPRETER CONVERGENCE SWEEP for ``normalize_origin``.

THE DEFECT CLASS THIS EXISTS FOR
================================
``normalize_origin`` is duplicated BY VALUE in two services that share no
package: ``qe-explorer/app/attest.py`` and
``qe-central/app/services/walk_attestation.py``. The invariant is "fix both or
pin identical".

At 14b3957 that invariant was broken by the RUNTIME rather than by the source.
The two copies were byte-identical and still behaved differently, because
``urlsplit`` changed between CPython versions:

    'https://[example.test]/x'   ->  'https://example.test'   on 3.10
    'https://[example.test]/x'   ->  ''                       on 3.11

qe-central runs 3.11; qe-explorer ships a CPython 3.10 image
(``mcr.microsoft.com/playwright/python``). So one service would mint an origin
the other refuses — a total walk-persistence outage presenting as
``origin_mismatch`` on a correctly-provisioned environment.

WHY THE EXISTING HARNESS COULD NOT SEE IT
=========================================
``run_certification.sh`` pins a single interpreter, so its AGREEMENT invariant
is structurally incapable of observing runtime divergence. On the failing case
the two copies AGREED under 3.10 and AGREED under 3.11 — the disagreement
existed only BETWEEN versions. A within-interpreter assertion passes on both and
reports green.

**That is the detail most likely to be got wrong when re-implementing this.** A
matrix that only re-runs the within-interpreter check on two Pythons is a job
that looks like the fix and is not one. The cross-version comparison is the
entire point.

WHAT THIS SCRIPT DOES
=====================
Emits, for ONE interpreter, the result of both copies over a frozen vector
table. ``--compare`` then takes two such files and asserts:

  1. WITHIN each interpreter  — the two copies agree on every vector;
  2. ACROSS both interpreters — each copy gives the same answer on both;
  3. IDEMPOTENCE              — ``N(N(x)) == N(x)`` everywhere.

DEPENDENCY-FREE ON PURPOSE. It extracts the function's SOURCE and execs it with
nothing but ``urllib.parse``. It imports neither service, so it cannot fail for
a dependency reason unrelated to the invariant, and it runs under any CPython
without installing either service's requirements.
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

#: Relative to the repository root (the directory containing ``Nexus_power``).
COPIES = {
    "explorer": "Nexus_power/engines/qe-explorer/app/attest.py",
    "central": "Nexus_power/platform/qe-central/app/services/walk_attestation.py",
}

#: FROZEN VECTOR TABLE. Both interpreters must be fed identical input or the
#: comparison is meaningless. Grouped by what each row is probing.
VECTORS: tuple[str, ...] = (
    # -- the 14b3957 case: a BRACKETED NON-IP literal. This is the row that
    #    diverged. Keep it first; if the sweep only ever runs one vector, this
    #    is the one worth running.
    "https://[example.test]/x",
    "https://[not-an-ip]/",
    "http://[localhost]:8080/x",
    # -- genuine IPv6 literals (CERT-FINDING-2: brackets must be restored)
    "https://[2001:db8::1]:8443/apply",
    "https://[::1]/apply",
    "http://[::1]:80/x",
    "https://[2001:db8::1]/x",
    "http://[2001:db8::1]:8080/",
    "https://[::ffff:192.0.2.1]/x",
    "https://[2001:0db8:0000:0000:0000:0000:0000:0001]:9443/x",
    "https://[fe80::1%25eth0]:443/x",
    # -- ordinary hosts, including default-port elision
    "https://example.test/x",
    "https://example.test:443/x",
    "http://example.test:80/x",
    "https://example.test:8443/x",
    "HTTPS://EXAMPLE.TEST/x",
    # -- malformed / negative controls: every one must normalise to ""
    "https://example.test:99999/x",
    "https://example.test:notaport/x",
    "https://[2001:db8::1/x",          # unclosed bracket
    "https://]2001:db8::1[/x",         # inverted brackets
    "not a url",
    "",
    "://missing-scheme/x",
    "https:///empty-host",
)


def _load(path: pathlib.Path):
    """Extract ``normalize_origin`` from a source file and exec it in isolation.

    Deliberately NOT an import: importing either service pulls in its package
    (and both ship a top-level ``app``, which collide), and would make this
    sweep depend on requirements it has no need for.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "normalize_origin":
            ns: dict = {}
            exec("from urllib.parse import urlsplit\n"
                 + ast.get_source_segment(src, node), ns)
            return ns["normalize_origin"]
    raise SystemExit(f"normalize_origin not found in {path}")


def sweep(root: pathlib.Path) -> dict:
    fns = {name: _load(root / rel) for name, rel in COPIES.items()}
    rows: dict = {}
    for vector in VECTORS:
        row: dict = {}
        for name, fn in fns.items():
            try:
                once = fn(vector)
                twice = fn(once)
                row[name] = {"once": once, "idempotent": once == twice}
            except Exception as exc:                      # a raise is a result
                row[name] = {"once": f"!RAISED:{type(exc).__name__}",
                             "idempotent": False}
        rows[vector] = row
    return {"python": ".".join(str(p) for p in sys.version_info[:3]),
            "vectors": rows}


def compare(paths: list[pathlib.Path]) -> int:
    """Assert the three invariants across every supplied sweep. Returns exit code."""
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    if len(runs) < 2:
        print("CONVERGENCE: need at least two interpreter sweeps to compare",
              file=sys.stderr)
        return 1

    failures: list[str] = []
    versions = [r["python"] for r in runs]
    print(f"interpreters compared: {', '.join(versions)}")
    print(f"vectors per interpreter: {len(VECTORS)}\n")

    for vector in VECTORS:
        # 1 + 3: within each interpreter, and idempotence.
        for run in runs:
            row = run["vectors"].get(vector)
            if row is None:
                failures.append(f"[{run['python']}] {vector!r}: missing from sweep")
                continue
            a, b = row["explorer"], row["central"]
            if a["once"] != b["once"]:
                failures.append(
                    f"WITHIN {run['python']}: {vector!r} -> explorer={a['once']!r} "
                    f"central={b['once']!r}")
            for copy_name, res in row.items():
                if not res["idempotent"]:
                    failures.append(
                        f"IDEMPOTENCE {run['python']} {copy_name}: {vector!r} -> "
                        f"{res['once']!r} does not re-normalise to itself")

        # 2: ACROSS interpreters — the half the single-interpreter harness
        #    cannot see, and the reason this job exists.
        for copy_name in COPIES:
            answers = {r["python"]: r["vectors"][vector][copy_name]["once"]
                       for r in runs if vector in r["vectors"]}
            if len(set(answers.values())) > 1:
                rendered = "  ".join(f"{v}={a!r}" for v, a in answers.items())
                failures.append(
                    f"ACROSS interpreters, {copy_name}: {vector!r} -> {rendered}")

    if failures:
        print(f"CONVERGENCE FAILURES: {len(failures)}\n", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        print("\nnormalize_origin is duplicated by value in two services that run "
              "DIFFERENT CPython versions. A divergence here means one service "
              "mints an origin the other refuses — walk persistence fails fleet-"
              "wide, presenting as origin_mismatch on a healthy environment.",
              file=sys.stderr)
        return 1

    print(f"CONVERGENCE OK: {len(VECTORS)} vectors x {len(COPIES)} copies x "
          f"{len(runs)} interpreters — agree within each and across all.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".",
                    help="repository root (the directory containing Nexus_power)")
    ap.add_argument("--out", help="write this interpreter's sweep here (JSON)")
    ap.add_argument("--compare", nargs="+", metavar="SWEEP.json",
                    help="compare two or more sweep files and assert convergence")
    args = ap.parse_args()

    if args.compare:
        return compare([pathlib.Path(p) for p in args.compare])

    result = sweep(pathlib.Path(args.root))
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        pathlib.Path(args.out).write_text(payload, encoding="utf-8")
        print(f"sweep written for Python {result['python']} -> {args.out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

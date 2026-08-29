"""Crawl a target described by DATA, not by code.

WHY THIS EXISTS. ``gate2_journey.py`` is the Gate-2 harness and its ``APPS``
table is deliberately part of the gate: three named proving grounds, their
declared credentials, and the one control each is authorised to cross. That is
correct for a gate and wrong for everything else — adding a fourth application
to it meant editing Python, which is how LifeOps was crawled and is not a way to
reach a hundred applications, let alone a thousand.

This runner takes the same machinery and reads its targets from a JSON file, so
onboarding an application is a config change a non-programmer can make and
review. Nothing about the crawl differs: it calls ``gate2_journey.run`` and
``gate2_journey.verdict_of`` directly, so the engine, the guard, the refuse
pack, the evidence bundle and the provenance stamp are byte-for-byte the ones
the gate uses. Only WHERE the target description comes from has changed.

    python crawl_target.py --config targets.json                 # every target
    python crawl_target.py --config targets.json --only keycloak # just one

Config shape — a list under ``targets``, each entry:

    {
      "name":        "keycloak",                  required, used for the bundle dir
      "url":         "http://127.0.0.1:3011/",    required, the served root
      "commit":      "Delete",                    the ONE control a grant authorises,
                                                  "" for a read-only crawl
      "max_crossings": 1,                          how many times, default 1
      "credentials": {"username": "...", "password": "..."},   omit for public
      "max_states":  120,
      "max_duration_ms": 900000,
      "oracle":      "stub" | "live"
    }

A target with no ``commit`` is crawled with NO boundary grant. That is the safe
default and it is the honest one: an irreversible control is crossed only where
somebody wrote down which control, and how many times.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Mapping

import gate2_journey as G


def _register(target: Mapping[str, Any]) -> str:
    """Put one config entry into the harness's own APPS table and return its key.

    The harness reads ``commit`` / ``credentials`` / ``transits`` from that
    table, so registering the target is all that is needed for every downstream
    behaviour — including ``grants_for``, which is what turns ``commit`` into
    the single named boundary approval.
    """
    name = str(target.get("name") or "").strip()
    if not name:
        raise SystemExit("each target needs a name")
    G.APPS[name] = {
        # A label no control carries authorises nothing — the read-only default.
        "commit": str(target.get("commit") or "__no_grant__"),
        "transits": [],
        "credentials": target.get("credentials") or None,
        "container_port": int(target.get("container_port") or 0),
    }
    return name


def _grants_with_budget(original, budget: int):
    """Wrap ``grants_for`` so a target may authorise N crossings from config.

    The harness hard-codes ``max_crossings: 1``; an application that gates on
    signing three documents needs three, and that is a property of the TARGET,
    not of the code.
    """
    def _wrapped(app: str) -> list[dict[str, Any]]:
        grants = original(app)
        for g in grants:
            g["max_crossings"] = max(1, int(budget))
        return grants
    return _wrapped


def crawl_one(target: Mapping[str, Any], out_root: Path) -> dict[str, Any]:
    name = _register(target)
    url = str(target.get("url") or "").strip()
    if not url:
        raise SystemExit(f"target {name!r} has no url")

    out = out_root / name
    out.mkdir(parents=True, exist_ok=True)

    original_grants = G.grants_for
    G.grants_for = _grants_with_budget(original_grants,
                                       int(target.get("max_crossings") or 1))
    started = time.time()
    try:
        result = asyncio.run(G.run(
            name, url,
            oracle_kind=str(target.get("oracle") or "stub"),
            out_root=out,
            max_states=int(target.get("max_states") or 120),
            max_duration_ms=int(target.get("max_duration_ms") or 900000),
        ))
        verdict = G.verdict_of(name, url, result)
    finally:
        G.grants_for = original_grants

    (out / "coverage.json").write_text(
        json.dumps(result["coverage"], indent=2, default=str, sort_keys=True),
        encoding="utf-8")
    (out / "journey.json").write_text(
        json.dumps(verdict, indent=2, default=str), encoding="utf-8")

    cov = result["coverage"]
    states = cov.get("states") or []
    seen: set[str] = set()
    for s in states:
        seen.update((s.get("form_snapshot_signals") or {}))
    filled = {e.get("name") for e in (cov.get("field_ledger") or [])}
    return {
        "name": name,
        "url": url,
        "elapsed_s": round(time.time() - started, 1),
        "pages": len(states),
        "controls": sum(s.get("controls_total", 0) for s in states),
        "fields_seen": len(seen),
        "fields_filled": len(filled & seen),
        "forms_found": cov.get("forms_found", 0),
        "forms_submitted": cov.get("forms_submitted", 0),
        "flows": len(cov.get("flows") or []),
        "deepest_flow": max([len(f.get("steps") or [])
                             for f in (cov.get("flows") or [])] or [0]),
        "journeys_verified": cov.get("journeys_completed", 0),
        "boundaries_crossed": cov.get("boundaries_crossed", 0),
        "risky_controls_seen": len({b.get("label")
                                    for b in (cov.get("approvable_boundary") or [])}),
        # NOT `not auth_incomplete`. Those are different questions, and the
        # gap between them printed `auth=True` for a crawl that never got in.
        #
        # MEASURED (ERPNext v16, 2026-08-28): the crawl drove the login form
        # correctly -- typed the email, typed the password, submitted -- and the
        # application refused it, so the crawl stopped `auth_failed` with one
        # state. `auth_incomplete` stayed False, deliberately: app/coverage.py
        # reserves that flag for a crawl that found NO login form and explored
        # public content anyway, which is a different terminal with partial
        # coverage to report. Conflating the two would mislead in the other
        # direction, and tests/test_crawler_logic.py pins that distinction.
        #
        # The summary asks a third question -- "did this crawl end up
        # authenticated?" -- and for `auth_failed` the answer is no, whatever
        # the flag says. Read the terminal, not one flag that never claimed to
        # answer this.
        "authenticated": (not cov.get("auth_incomplete")
                          and _stop_reason_of(cov) != "auth_failed"),
        "auth_reason": (cov.get("auth_reason") or "")[:90],
        "stop_reason": cov.get("summary", {}).get("stop_reason", "")
        if isinstance(cov.get("summary"), dict) else "",
        "evidence": str(out),
    }



def _stop_reason_of(cov: dict) -> str:
    """The crawl's terminal, wherever the bundle happens to carry it."""
    summary = cov.get("summary")
    if isinstance(summary, dict) and summary.get("stop_reason"):
        return str(summary["stop_reason"])
    return str(cov.get("stop_reason") or "")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--only", default="", help="crawl just this target name")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    targets = [t for t in (cfg.get("targets") or [])
               if not args.only or str(t.get("name")) == args.only]
    if not targets:
        raise SystemExit("no targets matched")

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for t in targets:
        name = t.get("name")
        print(f"\n{'=' * 62}\n=== {name}  {t.get('url')}\n{'=' * 62}", flush=True)
        try:
            rows.append(crawl_one(t, out_root))
        except Exception as exc:                      # one target must not end the run
            print(f"!! {name} FAILED: {exc}", flush=True)
            traceback.print_exc()
            rows.append({"name": name, "url": t.get("url"), "error": str(exc)[:200]})

    (out_root / "summary.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print("\n\n===================== SUMMARY =====================")
    for r in rows:
        if r.get("error"):
            print(f"  {r['name']:14} ERROR {r['error'][:70]}")
            continue
        print(f"  {r['name']:14} pages={r['pages']:3} fields={r['fields_filled']}/"
              f"{r['fields_seen']:<3} forms={r['forms_found']:2} "
              f"deepest={r['deepest_flow']:2} crossed={r['boundaries_crossed']} "
              f"verified={r['journeys_verified']} auth={r['authenticated']}")
    print(f"\n  summary: {out_root / 'summary.json'}")


if __name__ == "__main__":
    sys.exit(main())

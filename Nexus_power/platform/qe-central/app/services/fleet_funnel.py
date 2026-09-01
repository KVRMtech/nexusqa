"""FLEET FUNNEL — where crawls die, aggregated.

The architecture's honesty is PER-CRAWL and always has been: every crawl writes a
precise diagnosis, an honest stop reason, a posture, a generation outcome. No
surface ever aggregated them. So a fleet-wide collapse — weekly test yield 86% to
16%, four apps consuming 450 of 563 crawls, 270 crawls generating nothing —
surfaced as a founder escalation two months later rather than as a number that
moved.

"Where are we failing?" should be a dashboard, not an investigation.

This module is pure aggregation over ``qe_explorations.stats``. It computes
nothing new and estimates nothing: every field it reads is already written by the
crawl that produced it. That is deliberate — a telemetry layer that derives its
own numbers can disagree with the evidence, and then nobody trusts either.

THE FUNNEL, in the order a crawl passes through it. Each stage is a place a crawl
can die, and the drop between two stages is the thing worth looking at:

    dispatched -> completed -> pages captured -> forms found
              -> journeys walked -> tests generated -> deep enough to be an E2E

A stage that loses most of its input is where the engineering should go next.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Terminal states that mean the crawl produced evidence.
PRODUCTIVE = "completed"
#: Everything a crawl can end as, grouped for the funnel's first stage.
TERMINAL_BAD = frozenset({"failed", "refused", "cancelled", "error", "stalled"})


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _m(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def crawl_row(exploration: Mapping[str, Any]) -> dict[str, Any]:
    """One crawl projected onto the funnel's axes. Reads only; never derives."""
    stats = _m(exploration.get("stats"))
    coverage = _m(stats.get("coverage"))
    generate = _m(stats.get("generate"))
    flow = _m(coverage.get("flow_summary"))
    status = str(exploration.get("status") or "").strip().lower()

    generated = _int(generate.get("generated"))
    # B4 gave generation three distinguishable outcomes; older rows predate it,
    # so it is inferred here the same way B4 does — never guessed differently.
    outcome = str(generate.get("outcome") or "").strip()
    if not outcome:
        if generated > 0:
            outcome = "generated"
        elif str(generate.get("no_cases_reason") or "").strip():
            outcome = "no_cases"
        elif generate.get("attempted"):
            outcome = "unexplained"
        else:
            outcome = "not_attempted"

    return {
        "app_id": str(exploration.get("app_id") or ""),
        "status": status,
        "productive": status == PRODUCTIVE,
        "traversal": str(coverage.get("traversal") or "") or "(pre-posture)",
        "degraded": bool(_m(stats.get("degraded"))),
        "pages": _int(stats.get("visits")),
        "forms": _int(coverage.get("forms_found")),
        "flows": _int(flow.get("flows_found")),
        "deepest_flow": _int(flow.get("deepest_flow_steps")),
        # ── A19 · THE THREE FIELDS THAT MAKE `deepest_flow` READABLE ────────
        # `deepest_flow` alone cannot be read, and the explorer says so where it
        # computes it: "Six steps because the application has six, and six steps
        # because the walk was cut off at six, are the same integer and opposite
        # facts." It emits three more fields to separate them -- and until now
        # NOTHING in this service read any of them, so every fleet report showed
        # exactly the ambiguous number they exist to disambiguate.
        #
        # Measured on the Gate 2 crawls: vkpower-life reported deepest_flow=10,
        # which reads as deep coverage and was actually proven=0, capped=true,
        # terminal="loop" -- a truncated traversal that proved nothing.
        "deepest_flow_proven": _int(flow.get("deepest_flow_proven_steps")),
        #: Whether this row can be judged on PROVEN depth at all. A crawl
        #: recorded before the explorer emitted the field carries no opinion
        #: about it, and reading its absence as "proved nothing" would delete
        #: every historical crawl from the E2E stage on the day this shipped --
        #: a reporting change masquerading as a fleet-wide regression.
        "deepest_flow_proven_known": "deepest_flow_proven_steps" in flow,
        "deepest_flow_capped": bool(flow.get("deepest_flow_capped")),
        "deepest_flow_terminal": str(flow.get("deepest_flow_terminal") or ""),
        "advances": sum(_int(v) for v in _m(flow.get("advances_by_tier")).values()),
        "tests": generated,
        "generate_outcome": outcome,
        "auth_blocked": str(coverage.get("auth_blocked") or "").lower() == "true"
                        or coverage.get("auth_blocked") is True,
        "advance_blocked": len(coverage.get("advance_blocked") or []),
        "no_cases_reason": str(generate.get("no_cases_reason") or "")[:300],
    }


def summarize(explorations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The fleet funnel over a set of crawls.

    Returns stages (with the DROP between each), the yield, and the two
    breakdowns that answer "why": where crawls died, and which apps are burning
    capacity without producing anything.
    """
    rows = [crawl_row(e) for e in (explorations or ()) if isinstance(e, Mapping)]
    n = len(rows)
    if not n:
        return {"crawls": 0, "stages": [], "yield_pct": 0.0,
                "by_stop": {}, "zero_yield_apps": [], "notes": []}

    productive = [r for r in rows if r["productive"]]
    with_pages = [r for r in productive if r["pages"] > 0]
    with_forms = [r for r in with_pages if r["forms"] > 0]
    with_flows = [r for r in with_forms if r["flows"] > 0]
    with_tests = [r for r in productive if r["tests"] > 0]
    # "Deep enough to be an E2E" — a journey that actually walked more than its
    # entry page. This is the stage the whole product turns on, and separating it
    # keeps a fleet of one-step journeys from reading as success.
    # A19: PROVEN depth, not walked depth. `deepest_flow` counts the steps the
    # deepest walk TOOK, which includes a walk that was cut off by a budget --
    # so a fleet of truncated traversals scored identically to a fleet of
    # completed journeys at this stage, the one "the whole product turns on".
    # `deepest_flow_proven` counts only walks that reached a genuine end.
    def _proven_depth(r: Mapping[str, Any]) -> int:
        """PROVEN depth where the crawl states one; walked depth where it cannot.

        Pre-hardening rows fall back rather than being judged on a field they
        never carried — see `deepest_flow_proven_known`."""
        return (r["deepest_flow_proven"] if r["deepest_flow_proven_known"]
                else r["deepest_flow"])

    deep = [r for r in with_flows if _proven_depth(r) > 1]
    # Kept visible rather than folded away: the crawls that LOOK deep and are
    # only deep because nothing stopped them yet.
    capped_only = [r for r in with_flows
                   if r["deepest_flow"] > 1 and _proven_depth(r) <= 1]

    def stage(name: str, kept: list, prior: int) -> dict[str, Any]:
        lost = max(prior - len(kept), 0)
        return {
            "stage": name,
            "crawls": len(kept),
            "lost_here": lost,
            "lost_pct": round(100.0 * lost / prior, 1) if prior else 0.0,
        }

    stages = [
        {"stage": "dispatched", "crawls": n, "lost_here": 0, "lost_pct": 0.0},
        stage("completed", productive, n),
        stage("captured pages", with_pages, len(productive)),
        stage("found forms", with_forms, len(with_pages)),
        stage("walked journeys", with_flows, len(with_forms)),
        stage("generated tests", with_tests, len(with_flows)),
        stage("deep enough for E2E", deep, len(with_tests)),
    ]

    by_stop: dict[str, int] = {}
    for r in rows:
        key = r["status"] if not r["productive"] else r["generate_outcome"]
        by_stop[key] = by_stop.get(key, 0) + 1

    # Apps burning capacity for nothing — the C1 brake stops the loop, this names
    # who was in it. Sorted by waste, because that is the order to look.
    per_app: dict[str, dict[str, int]] = {}
    for r in rows:
        a = per_app.setdefault(r["app_id"], {"crawls": 0, "tests": 0})
        a["crawls"] += 1
        a["tests"] += r["tests"]
    zero_yield = sorted(
        ({"app_id": k, "crawls": v["crawls"]} for k, v in per_app.items()
         if v["tests"] == 0 and v["crawls"] >= 3),
        key=lambda x: -x["crawls"])[:10]

    notes: list[str] = []
    unexplained = by_stop.get("unexplained", 0)
    if unexplained:
        notes.append(
            f"{unexplained} crawl(s) generated nothing AND gave no reason — that "
            f"is a gap in the generator, not a finding about those applications")
    blocked = sum(1 for r in rows if r["advance_blocked"])
    if blocked:
        notes.append(
            f"{blocked} crawl(s) hit a forward control the app had disabled — the "
            f"named missing fields are the highest-value thing to supply")
    degraded = sum(1 for r in rows if r["degraded"])
    if degraded:
        notes.append(f"{degraded} crawl(s) ran at a lower posture than requested")
    # A19 — the crawls that LOOK deep and are only deep because a budget ran out
    # before the funnel did. Named, because the "deep enough for E2E" stage above
    # now excludes them and a stage that silently drops crawls is how a fleet
    # report stops being believed.
    if capped_only:
        notes.append(
            f"{len(capped_only)} crawl(s) walked more than one step but proved "
            f"none of it — the deepest walk was cut off by a budget, not by the "
            f"end of the funnel, so their depth is a floor and not a measurement")

    return {
        "crawls": n,
        "stages": stages,
        # THE headline: productive crawls that produced a test.
        "yield_pct": round(100.0 * len(with_tests) / n, 1),
        "e2e_capable_pct": round(100.0 * len(deep) / n, 1),
        #: A19 — crawls whose depth is a floor rather than a measurement. Kept
        #: beside the percentage it was removed from, so the two are read together.
        "capped_depth_crawls": len(capped_only),
        "by_stop": dict(sorted(by_stop.items(), key=lambda kv: -kv[1])),
        "zero_yield_apps": zero_yield,
        "notes": notes,
    }


def worst_stage(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The stage losing the most crawls — where the next engineering should go.

    The measure-first loop in one function: rather than arguing about priorities,
    read which stage drops the most and fix that one.
    """
    stages = [s for s in (summary.get("stages") or ()) if _int(s.get("lost_here"))]
    if not stages:
        return {}
    return max(stages, key=lambda s: _int(s.get("lost_here")))


__all__ = ["crawl_row", "summarize", "worst_stage", "PRODUCTIVE", "TERMINAL_BAD"]

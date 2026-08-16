"""EXECUTION COVERAGE for the injected capture JavaScript.

Coverage is measured with **V8 precise coverage** over a raw CDP session while
the whole fixture library is walked through the production injection path. That
choice matters:

  * it measures the JavaScript **as Chromium actually executed it**, not a
    transpiled or instrumented copy;
  * it needs **no modification of the snippet** — no `//# sourceURL`, no
    wrapper, no instrumentation pass. The script is located by matching its
    SOURCE, fetched back from the debugger, against the production constant. The
    bytes that ran are the bytes in `app/inventory_js.py`;
  * `detailed: true` gives **block-level** ranges, which is what V8 (and
    therefore c8/nyc) report as branch coverage — every `if` arm, every `||`
    short-circuit, every `catch` is its own range.

Three numbers are reported, all restricted to the byte window that the
production constant occupies inside the evaluated script:

  functions   — functions entered at least once
  blocks      — V8 coverage ranges entered at least once  (the BRANCH metric)
  lines       — source lines with at least one covered byte

The gate is on **blocks ≥ 70%**, per the milestone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

#: Where the machine-readable coverage report is written for CI to archive.
COVERAGE_REPORT = H.HERE / "coverage_report.json"

#: The milestone gate.
BRANCH_TARGET = 70.0

#: A stable, distinctive fragment used to identify the evaluated script among
#: every script Chromium parsed. Chosen because it appears exactly once and is
#: unlikely to collide with page code.
_FINGERPRINTS = {
    "INVENTORY_JS": "function frameSelectorFor(iframeEl, index)",
    "OPAQUE_JS": 'push("cross_origin_iframe"',
    "DISPLAYED_VALUES_JS": "function ownText(el)",
}


def _measure(pw, fixture_server, snippet: str) -> dict[str, Any]:
    """Walk every fixture with V8 precise coverage on, and attribute it."""
    source = H.production_snippet(snippet)
    needle = source.strip()
    fingerprint = _FINGERPRINTS[snippet]
    what = {"INVENTORY_JS": "controls", "OPAQUE_JS": "opaque",
            "DISPLAYED_VALUES_JS": "displayed_values"}[snippet]

    scripts: dict[str, dict[str, Any]] = {}

    async def _run() -> list[dict[str, Any]]:
        cdp = await pw.context.new_cdp_session(pw.page)

        def _on_parsed(event: dict[str, Any]) -> None:
            scripts[event["scriptId"]] = event

        cdp.on("Debugger.scriptParsed", _on_parsed)
        await cdp.send("Debugger.enable")
        await cdp.send("Profiler.enable")
        await cdp.send("Profiler.startPreciseCoverage",
                       {"callCount": True, "detailed": True})

        async def _take() -> list[dict[str, Any]]:
            """Harvest + identify the scripts alive in the CURRENT context."""
            result = await cdp.send("Profiler.takePreciseCoverage")
            found = []
            for entry in result.get("result", []):
                try:
                    src = (await cdp.send("Debugger.getScriptSource",
                                          {"scriptId": entry["scriptId"]}))["scriptSource"]
                except Exception:
                    continue                  # script already collected
                if fingerprint not in src:
                    continue
                offset = src.find(needle)
                if offset < 0:
                    # The production text is present but not verbatim contiguous
                    # — refuse to guess an attribution window rather than report
                    # a number that might be measuring something else.
                    continue
                entry["_offset"] = offset
                found.append(entry)
            return found

        # Drive EVERY fixture through the production port, harvesting coverage
        # BEFORE navigating away. A `goto` discards the previous execution
        # context and its scripts with it, so a single harvest at the end would
        # only ever see the LAST fixture — which is exactly what the first
        # measurement did: it reported 38.85% and claimed the native-<select>
        # option loop never ran, while fixture 07 provably captures 576 options
        # through it. Coverage that contradicts a passing test is a measurement
        # bug, not a coverage result.
        ours: list[dict[str, Any]] = []
        for name in H.fixture_names():
            spec = H.fixture_spec(name)
            if "playwright" not in spec.get("lanes", []):
                continue
            await H.collect_via_production_port(
                pw.page, pw.context, fixture_server.url(name), what=what)
            ours.extend(await _take())

        await cdp.send("Profiler.stopPreciseCoverage")
        await cdp.detach()
        return ours

    entries = pw.run(_run())
    assert entries, (
        f"V8 reported no coverage for {snippet}: the evaluated script could not "
        f"be located by source match. Coverage cannot be attributed, and a "
        f"number produced anyway would be measuring the wrong thing.")

    # ── attribute ranges to the production-constant window ───────────────────
    #
    # EVERY `page.evaluate(INVENTORY_JS)` mints a NEW V8 script, so walking 13
    # fixtures yields 13 script entries of identical source. They must be
    # UNIONED by offset-relative-to-their-own-base, never summed: summing counts
    # one logical function once per fixture and marks it uncovered in the twelve
    # fixtures that did not happen to reach it, which understates coverage by
    # roughly the fixture count. (First measurement said 38.85% and claimed the
    # native-<select> option loop never ran, while fixture 07 provably captures
    # 576 options through it — that contradiction is what exposed this.)
    # V8 emits range BOUNDARIES that depend on which branches a given page
    # actually took, so two instances of the same script report different range
    # splits. Covered bytes must therefore be resolved PER INSTANCE — inner
    # uncovered ranges subtracted from their own enclosing covered range — and
    # only then unioned. Subtracting across instances instead lets an uncovered
    # range from the canvas fixture erase bytes the select fixture covered, which
    # is what made `if (tag === "button") return "button"` read as never executed
    # on a fixture library where every page has buttons.
    window_len = len(needle)
    func_hits: dict[tuple[int, int], bool] = {}
    all_ranges: set[tuple[int, int]] = set()
    covered_bytes: set[int] = set()

    for entry in entries:
        base = entry["_offset"]
        lo, hi = base, base + window_len
        inst_covered: set[int] = set()
        inst_uncovered: set[int] = set()
        for fn in entry["functions"]:
            ranges = fn.get("ranges") or []
            if not ranges or not (lo <= ranges[0]["startOffset"] < hi):
                continue                       # a function outside our window
            fkey = (ranges[0]["startOffset"] - base, ranges[0]["endOffset"] - base)
            func_hits[fkey] = func_hits.get(fkey, False) or ranges[0]["count"] > 0
            for rng in ranges:
                start = rng["startOffset"] - base
                end = rng["endOffset"] - base
                all_ranges.add((start, end))
                span = range(max(start, 0), min(end, window_len))
                (inst_covered if rng["count"] > 0 else inst_uncovered).update(span)
        covered_bytes |= (inst_covered - inst_uncovered)

    total_funcs = len(func_hits)
    covered_funcs = sum(1 for v in func_hits.values() if v)

    # A block counts as covered when its first byte is covered somewhere in the
    # library. Derived from the same byte union as the line metric, so the two
    # numbers cannot disagree about whether a given branch ever ran.
    total_blocks = len(all_ranges)
    covered_blocks = sum(1 for (start, _end) in all_ranges if start in covered_bytes)

    # ── line coverage over the production constant ───────────────────────────
    line_starts, pos = [], 0
    for line in needle.splitlines(keepends=True):
        line_starts.append((pos, pos + len(line), line))
        pos += len(line)
    total_lines = covered_lines = 0
    uncovered_lines: list[int] = []
    for idx, (start, end, text) in enumerate(line_starts, start=1):
        if not text.strip():
            continue                            # blank lines are not statements
        total_lines += 1
        if any(b in covered_bytes for b in range(start, end)):
            covered_lines += 1
        else:
            uncovered_lines.append(idx)

    def pct(n: int, d: int) -> float:
        return round(100.0 * n / d, 2) if d else 0.0

    return {
        "snippet": snippet,
        "functions": {"covered": covered_funcs, "total": total_funcs,
                      "pct": pct(covered_funcs, total_funcs)},
        "blocks": {"covered": covered_blocks, "total": total_blocks,
                   "pct": pct(covered_blocks, total_blocks)},
        "lines": {"covered": covered_lines, "total": total_lines,
                  "pct": pct(covered_lines, total_lines)},
        "uncovered_line_numbers": uncovered_lines,
    }


@pytest.fixture(scope="session")
def coverage_report(pw, fixture_server) -> dict[str, Any]:
    report = {snippet: _measure(pw, fixture_server, snippet)
              for snippet in ("INVENTORY_JS", "OPAQUE_JS", "DISPLAYED_VALUES_JS")}
    COVERAGE_REPORT.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    return report


def test_inventory_js_branch_coverage_meets_the_gate(coverage_report) -> None:
    """≥70% BLOCK (branch) coverage of the production walker, from real runs."""
    inv = coverage_report["INVENTORY_JS"]
    assert inv["blocks"]["pct"] >= BRANCH_TARGET, (
        f"INVENTORY_JS block/branch coverage is {inv['blocks']['pct']}% "
        f"({inv['blocks']['covered']}/{inv['blocks']['total']} ranges), below the "
        f"{BRANCH_TARGET}% gate.\n"
        f"functions {inv['functions']['pct']}% · lines {inv['lines']['pct']}%\n"
        f"Uncovered source lines (1-based, within INVENTORY_JS): "
        f"{inv['uncovered_line_numbers'][:60]}")


def test_coverage_comes_from_executing_real_behaviour(coverage_report) -> None:
    """Guard against a number produced by trivial or synthetic execution.

    A high block percentage over a handful of functions would mean the walker
    barely ran. Requiring both a substantial function count and substantial
    function coverage keeps the headline number honest.
    """
    inv = coverage_report["INVENTORY_JS"]
    assert inv["functions"]["total"] >= 20, (
        f"V8 saw only {inv['functions']['total']} functions in INVENTORY_JS — "
        f"the attribution window is probably wrong")
    assert inv["functions"]["pct"] >= 80.0, (
        f"only {inv['functions']['pct']}% of the walker's functions were ever "
        f"entered; the block figure is not describing real execution")
    assert inv["lines"]["pct"] >= 60.0, (
        f"line coverage {inv['lines']['pct']}% is too low to call this "
        f"substantial execution of production logic")


#: Per-snippet floors, set just under the measured figures so a real regression
#: trips them while normal drift does not. OPAQUE_JS's block floor is the lowest
#: because its remaining uncovered ranges are almost entirely `catch` arms —
#: reachable only by inducing a DOM error — while its lines and functions are at
#: 100%. That gap is the honest ceiling, not a missing fixture.
FLOORS = {
    "INVENTORY_JS":        {"blocks": 78.0, "functions": 100.0, "lines": 99.0},
    "DISPLAYED_VALUES_JS": {"blocks": 73.0, "functions": 100.0, "lines": 99.0},
    "OPAQUE_JS":           {"blocks": 55.0, "functions": 100.0, "lines": 99.0},
}


@pytest.mark.parametrize("snippet", sorted(FLOORS), ids=sorted(FLOORS))
def test_every_snippet_holds_its_coverage_floor(coverage_report, snippet: str) -> None:
    """All three injected snippets are production capture and all three are held.

    Function coverage is pinned at 100% for each, which is a stronger statement
    than the block percentage: it means no part of the capture engine is
    unreachable by the fixture library. `test_the_walker_has_no_uncallable_functions`
    keeps it attainable by refusing to let dead code back in.
    """
    rep = coverage_report[snippet]
    floors = FLOORS[snippet]
    for metric, floor in floors.items():
        actual = rep[metric]["pct"]
        assert actual >= floor, (
            f"{snippet} {metric} coverage fell to {actual}% (floor {floor}%): "
            f"{rep[metric]['covered']}/{rep[metric]['total']}.\n"
            f"Uncovered lines: {rep['uncovered_line_numbers'][:40]}")


def test_coverage_report_is_written_for_ci(coverage_report) -> None:
    assert COVERAGE_REPORT.exists()
    data = json.loads(COVERAGE_REPORT.read_text(encoding="utf-8"))
    assert set(data) == {"INVENTORY_JS", "OPAQUE_JS", "DISPLAYED_VALUES_JS"}

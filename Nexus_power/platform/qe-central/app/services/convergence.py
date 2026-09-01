"""CONVERGENCE BRAKE — stop re-crawling an app that has stopped learning.

Live evidence, 18 days on verdict-box: 563 crawls, of which FOUR apps account for
450. One app was crawled 203 times. The overwhelming majority produced no new
pages and no tests, and nothing anywhere noticed — the autowalk planned the next
cycle, dispatched it, folded a result identical to the last one, and planned
again. The single-flight explorer was saturated by this, so real client crawls
were refused with "explorer busy" while the fleet burned its capacity
re-discovering what it already knew.

Every autonomous loop needs a brake. The one this module provides is deliberately
evidence-based rather than a counter: a crawl that finds NEW pages or produces
NEW tests is progress and resets the brake, however many crawls preceded it. Only
a genuine run of no-new-information cycles stops the sweep.

WHY NOT JUST CAP THE COUNT. An app with a large branch space legitimately needs
many cycles — capping at N would truncate real coverage and quietly under-report
it, which is the same green-wash in the other direction. Non-convergence is not
"many crawls"; it is "many crawls that changed nothing".

Pure functions over completed-crawl stats. No DB, no I/O, no LLM — the caller
loads the rows.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Consecutive no-new-information crawls before the sweep is braked. Three, not
#: one: a single barren cycle is ordinary (a branch walk that re-walked a known
#: path), two can be coincidence on a flaky app, three is a pattern. Small enough
#: that the 203-crawl loop would have stopped on day one.
DEFAULT_STUCK_AFTER = 3
#: Completed crawls a caller must load: the streak window PLUS one baseline row
#: (see :func:`assess` — without the extra row the brake is unreachable).
DEFAULT_LOOKBACK = DEFAULT_STUCK_AFTER + 1

STUCK_REASON = (
    "the last {n} crawls found no new pages and produced no new tests — "
    "re-running an identical crawl cannot produce a different result"
)


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def crawl_yield(stats: Mapping[str, Any] | None) -> dict[str, int]:
    """The INFORMATION one completed crawl produced: pages seen, tests made.

    ``visits`` is the page count the writer recorded; ``generated`` is the tests
    the factory produced. Both are already on every row — this reads them, it
    never recomputes or estimates.
    """
    s = stats if isinstance(stats, Mapping) else {}
    generate = s.get("generate")
    generate = generate if isinstance(generate, Mapping) else {}
    coverage = s.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    flow_summary = coverage.get("flow_summary")
    flow_summary = flow_summary if isinstance(flow_summary, Mapping) else {}
    return {
        "visits": _int(s.get("visits")),
        "generated": _int(generate.get("generated")),
        "forms": _int(coverage.get("forms_found")),
        "flows": _int(flow_summary.get("flows_found")),
    }


def is_barren(current: Mapping[str, int], best: Mapping[str, int]) -> bool:
    """True when this crawl learned NOTHING beyond the best already seen.

    Compared against the running BEST, not the immediately-previous crawl: two
    crawls can alternate between two partial results forever, each looking like
    progress relative to the one before it while the app's known surface never
    grows. Against the best, that pattern is correctly barren.
    """
    return all(_int(current.get(k)) <= _int(best.get(k)) for k in
               ("visits", "generated", "forms", "flows"))


def assess(
    completed_stats: Sequence[Mapping[str, Any] | None],
    *,
    stuck_after: int = DEFAULT_STUCK_AFTER,
) -> dict[str, Any]:
    """Should the sweep keep going? ``completed_stats`` is NEWEST-FIRST.

    Returns ``{stuck, barren_streak, reason, best}``. ``stuck`` is True only when
    the most recent ``stuck_after`` crawls each produced nothing beyond what was
    already known — a single informative crawl anywhere in that window clears it,
    because the app demonstrably still has something to teach us.

    THE CALLER MUST PASS MORE THAN ``stuck_after`` ROWS. The oldest row
    establishes the baseline; only the ones after it can be judged barren
    against it. Given exactly ``stuck_after`` rows the oldest would be measured
    against an empty baseline, look like progress whatever it contains, and the
    streak could never reach the threshold — the brake would be permanently
    unreachable. Caught by its own test on three identical crawls.

    An app without that much history is never stuck: there is not yet evidence of
    a pattern, and refusing early would block the ordinary case of a new app
    whose first crawls are legitimately thin.
    """
    rows = [s for s in (completed_stats or ())]
    if stuck_after < 1 or len(rows) <= stuck_after:
        return {"stuck": False, "barren_streak": 0, "reason": "", "best": {}}

    # Walk OLDEST-first so "best so far" means what it says.
    ordered = list(reversed(rows))
    best = {"visits": 0, "generated": 0, "forms": 0, "flows": 0}
    barren_flags: list[bool] = []
    for stats in ordered:
        y = crawl_yield(stats)
        barren_flags.append(is_barren(y, best))
        for k, v in y.items():
            if v > best[k]:
                best[k] = v

    streak = 0
    for flag in reversed(barren_flags):     # back to newest
        if not flag:
            break
        streak += 1

    if streak >= stuck_after:
        return {"stuck": True, "barren_streak": streak,
                "reason": STUCK_REASON.format(n=streak), "best": best}
    return {"stuck": False, "barren_streak": streak, "reason": "", "best": best}


__all__ = ["DEFAULT_STUCK_AFTER", "DEFAULT_LOOKBACK", "STUCK_REASON", "assess", "crawl_yield",
           "is_barren"]

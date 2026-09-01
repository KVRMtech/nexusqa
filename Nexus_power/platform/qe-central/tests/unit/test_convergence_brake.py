"""THE CONVERGENCE BRAKE — an autonomous loop with no brake is a spin.

Live evidence, 18 days on verdict-box: 563 crawls, of which FOUR apps account for
450. One app was crawled 203 times. Almost every cycle produced no new pages and
no new tests, and the autowalk planned the next one regardless — folding a result
identical to the last, then planning again. The single-flight explorer was
saturated by this, so REAL client crawls were refused "explorer busy" while the
fleet spent its whole capacity re-discovering what it already knew.

The brake is evidence-based, not a counter, and that distinction is the design:

  * a crawl that finds new pages or makes new tests is PROGRESS and clears the
    brake, however many crawls came before it — an app with a large branch space
    legitimately needs many cycles, and capping the count would truncate real
    coverage while quietly under-reporting it (the same green-wash, inverted);
  * "stuck" means many crawls that CHANGED NOTHING, which is the only thing that
    justifies refusing to try again.
"""
from __future__ import annotations

import pytest

from app.services.convergence import (
    DEFAULT_LOOKBACK,
    DEFAULT_STUCK_AFTER,
    assess,
    crawl_yield,
    is_barren,
)


def _stats(visits=0, generated=0, forms=0, flows=0):
    return {
        "visits": visits,
        "generate": {"generated": generated},
        "coverage": {"forms_found": forms, "flow_summary": {"flows_found": flows}},
    }


# ── the live shape ──────────────────────────────────────────────────────────

def test_the_203_crawl_loop_would_have_stopped_on_the_fourth():
    """THE DEFECT, in one assertion. Identical crawls, newest first: the OLDEST
    establishes what was already known, the three after it each add nothing."""
    barren = [_stats(visits=16, generated=0, forms=5, flows=6)] * 4
    out = assess(barren)
    assert out["stuck"] is True
    assert out["barren_streak"] == 3
    assert "no new pages" in out["reason"]


def test_the_threshold_window_alone_can_never_brake():
    """THE BUG THIS TEST CAUGHT. Given exactly ``stuck_after`` rows, the oldest
    is judged against an EMPTY baseline, looks like progress whatever it holds,
    and the streak can never reach the threshold — the brake would have been
    permanently unreachable. The caller must load a baseline row too, which is
    what DEFAULT_LOOKBACK exists for."""
    assert assess([_stats(visits=16, forms=5)] * 3)["stuck"] is False
    assert DEFAULT_LOOKBACK == DEFAULT_STUCK_AFTER + 1


def test_a_crawl_that_learned_something_clears_the_brake():
    """Progress is progress however long the barren run before it. The newest
    crawl found more pages, so the app demonstrably still has something to
    teach us and the sweep must continue."""
    rows = [
        _stats(visits=24, generated=12, forms=7, flows=8),   # newest: progress
        _stats(visits=16, generated=0, forms=5, flows=6),
        _stats(visits=16, generated=0, forms=5, flows=6),
        _stats(visits=16, generated=0, forms=5, flows=6),
    ]
    out = assess(rows)
    assert out["stuck"] is False
    assert out["barren_streak"] == 0


def test_new_tests_alone_count_as_progress():
    rows = [
        _stats(visits=16, generated=3, forms=5, flows=6),    # same pages, NEW tests
        _stats(visits=16, generated=0, forms=5, flows=6),
        _stats(visits=16, generated=0, forms=5, flows=6),
    ]
    assert assess(rows)["stuck"] is False


# ── the subtle case the "compare to previous" version gets wrong ───────────

def test_two_alternating_partial_results_are_correctly_barren():
    """THE REASON THE COMPARISON IS AGAINST THE RUNNING BEST.

    Two crawls can alternate between partial results forever, each looking like
    progress relative to the one immediately before it, while the app's known
    surface never grows. Against the BEST seen, that pattern is barren — which
    is what it actually is."""
    rows = [
        _stats(visits=10, generated=0, forms=5, flows=6),
        _stats(visits=16, generated=0, forms=3, flows=4),
        _stats(visits=10, generated=0, forms=5, flows=6),
        _stats(visits=16, generated=0, forms=5, flows=6),   # oldest: the best
    ]
    out = assess(rows, stuck_after=3)
    assert out["stuck"] is True, (
        "alternating partial results read as progress against the PREVIOUS "
        "crawl, and as barren against the best — which is what they are")


# ── never brake without evidence ───────────────────────────────────────────

@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_a_new_app_is_never_stuck_before_the_pattern_exists(n):
    """Refusing early would block the ordinary case of a new app whose first
    crawls are legitimately thin."""
    rows = [_stats(visits=16, generated=0, forms=5, flows=6)] * n
    assert assess(rows)["stuck"] is False


def test_junk_history_never_brakes_a_sweep():
    for rows in ([None, None, None], [{}, {}, {}],
                 [{"generate": "nope"}, {"coverage": 3}, None]):
        out = assess(rows)
        # Junk reads as zero-yield, which IS barren — but it must never crash,
        # and the caller treats an unreadable history as not-stuck anyway.
        assert isinstance(out["stuck"], bool)
        assert isinstance(out["barren_streak"], int)


def test_yield_reads_the_fields_that_already_exist_and_invents_none():
    y = crawl_yield(_stats(visits=24, generated=12, forms=7, flows=8))
    assert y == {"visits": 24, "generated": 12, "forms": 7, "flows": 8}
    assert crawl_yield(None) == {"visits": 0, "generated": 0, "forms": 0, "flows": 0}


def test_barren_is_strictly_no_better_on_every_axis():
    best = {"visits": 16, "generated": 0, "forms": 5, "flows": 6}
    assert is_barren({"visits": 16, "generated": 0, "forms": 5, "flows": 6}, best)
    assert not is_barren({"visits": 17, "generated": 0, "forms": 5, "flows": 6}, best)
    assert not is_barren({"visits": 16, "generated": 1, "forms": 5, "flows": 6}, best)


def test_the_default_threshold_is_small_enough_to_matter():
    """Three, not thirty: the 203-crawl loop must stop on day one, and a single
    barren cycle must not."""
    assert DEFAULT_STUCK_AFTER == 3


# ── the brake is actually wired into the loop ──────────────────────────────

def test_the_autowalk_consults_the_brake_before_dispatching():
    import inspect

    from app.routers import internal

    src = inspect.getsource(internal.complete_crawl)
    assert "_autowalk_convergence" in src
    loader = inspect.getsource(internal._autowalk_convergence)
    assert "DEFAULT_LOOKBACK" in loader, (
        "the caller loads only the streak window, so the brake is unreachable")
    assert 'not stuck["stuck"]' in src, (
        "the brake is computed but does not gate the dispatch")

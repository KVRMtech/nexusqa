"""MASTER CATALOGUE COMPLETENESS — every answer a question offers.

A catalogued question is only useful if it holds the ANSWER SET, because that
enumeration is what a generated positive / negative / boundary case is built from.
"What state do you live in?" has fifty-one answers; a catalogue holding forty of
them cannot produce a case about Wyoming, and — worse — nothing in the record said
eleven were missing.

Two separate problems, fixed separately here:

  1. BOUNDS. The read ceilings were sized for a probe (60 in the page, 40 in the
     open-probe). A country list has ~250 answers and a date-of-birth year range
     ~100. Under full traversal the ceilings are sized for a real answer set.
  2. HONESTY. Whatever the ceiling, a list that hits it must SAY SO. The captured
     labels and the offered COUNT are recorded separately, so "247 offered, 300
     captured" is a fact a consumer can act on, and a clipped list can never be
     mistaken for the complete set of valid answers.

(2) is the one that must hold in EVERY posture. A probe is allowed to capture
less; nothing is allowed to capture less and not say so.
"""
from __future__ import annotations

import asyncio

from app.config import Settings
from app.crawler import (
    _FULL_DEP_PROBES,
    _FULL_OPTION_PROBES,
    _FULL_PROBED_OPTIONS,
    _MAX_DEP_PROBES,
    _MAX_OPTION_PROBES,
    _MAX_PROBED_OPTIONS,
    TRAVERSAL_FULL,
    TRAVERSAL_PROBE,
    Budget,
    Crawler,
    GuardContext,
)
from app.guard import load_refuse_pack
from app.inventory import build_inventory
from app.inventory_js import INVENTORY_JS

_REFUSE = load_refuse_pack(Settings().refuse_pack_path)

#: The enumeration the requirement names by hand: every US state.
_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
]


def _crawler(tmp_path, **over) -> Crawler:
    kwargs = dict(
        crawl_id="c1", tenant_id="t1", target_url="https://app.example/",
        work_dir=str(tmp_path), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=4),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE),
    )
    kwargs.update(over)
    return Crawler(None, **kwargs)


def _select(name: str, options: list[str], total: int | None = None) -> dict:
    return {
        "role": "combobox", "name": name, "name_source": "label",
        "best_effort": False, "kind": "select", "tag": "select",
        "input_type": "", "options": list(options),
        "options_total": len(options) if total is None else total,
        "required": True, "disabled": False, "frame_selector": "",
        "testid": "", "css_hint": "", "value_committed": "",
        "landmark": {"role": "", "name": ""},
    }


# ── the enumeration the requirement names by hand ───────────────────────────

def test_a_full_state_dropdown_survives_the_inventory_intact(tmp_path):
    """Alabama through Wyoming, all fifty, with nothing dropped on the way in."""
    built = build_inventory([_select("What state do you live in?", _STATES)],
                            _REFUSE, url="https://app.example/apply")
    assert built[0]["options"] == _STATES
    assert built[0]["options_total"] == 50


def test_a_country_sized_list_is_no_longer_clipped_by_the_page_read():
    """~250 countries used to be read as 60. The ceiling in the injected JS is
    what decided that, so it is pinned here directly."""
    assert "var MAX_OPTIONS = 300;" in INVENTORY_JS


# ── HONESTY: a clipped list must say it is clipped ──────────────────────────

def test_a_clipped_enumeration_is_marked_not_silently_shortened(tmp_path):
    """THE GREEN-WASH THIS PREVENTS. A prefix presented as an answer set is a
    fabrication: every case generated from it would claim to cover a question it
    only partly knows."""
    c = _crawler(tmp_path, traversal=TRAVERSAL_PROBE)      # captures 40
    control: dict = {"name": "Country", "qec": {}}
    c._set_options(control, [f"Country {i}" for i in range(250)])

    assert len(control["options"]) == _MAX_PROBED_OPTIONS
    assert control["options_total"] == 250, "the offered COUNT must survive"
    assert control["options_truncated"] is True


def test_a_complete_enumeration_carries_no_truncation_marker(tmp_path):
    """The marker has to mean something, so it must be absent when the capture
    really is complete — otherwise a consumer learns to ignore it."""
    c = _crawler(tmp_path, traversal=TRAVERSAL_FULL)
    control: dict = {"name": "State", "qec": {}}
    c._set_options(control, _STATES)

    assert control["options"] == _STATES
    assert control["options_total"] == 50
    assert "options_truncated" not in control


def test_the_options_total_never_understates_what_was_captured(tmp_path):
    """A page is free to report nonsense. The count is floored at the number of
    labels actually read, so the record can never claim FEWER answers than it
    demonstrably holds."""
    built = build_inventory(
        [_select("State", _STATES, total=0)], _REFUSE, url="https://app.example/")
    assert built[0]["options_total"] == 50

    for junk in (None, "", "abc", -5, True, 3.7):
        b = build_inventory([_select("State", _STATES, total=junk)],
                            _REFUSE, url="https://app.example/")
        assert b[0]["options_total"] == 50, junk


def test_the_diagnostics_copy_matches_the_captured_list(tmp_path):
    """``qec.options`` is what evidence renders. It must never disagree with the
    control's own list, or the report and the catalogue tell different stories."""
    c = _crawler(tmp_path, traversal=TRAVERSAL_PROBE)
    control: dict = {"name": "Country", "qec": {"options": ["stale"]}}
    c._set_options(control, [f"C{i}" for i in range(100)])
    assert control["qec"]["options"] == control["options"]


# ── BOUNDS: sized for an answer set, still bounded ──────────────────────────

def test_full_traversal_captures_a_real_answer_set(tmp_path):
    c = _crawler(tmp_path, traversal=TRAVERSAL_FULL)
    assert c._max_probed_options == _FULL_PROBED_OPTIONS >= 250
    assert c._max_option_probes == _FULL_OPTION_PROBES
    assert c._max_dep_probes == _FULL_DEP_PROBES


def test_a_probe_keeps_the_old_bounds_exactly(tmp_path):
    """REGRESSION GUARD: an unattested app is sampled exactly as before."""
    c = _crawler(tmp_path)
    assert c._max_probed_options == _MAX_PROBED_OPTIONS
    assert c._max_option_probes == _MAX_OPTION_PROBES
    assert c._max_dep_probes == _MAX_DEP_PROBES


def test_completeness_is_bounded_in_both_postures():
    """"Complete" means "sized for a real answer set", never "unbounded" — one
    pathological control must not be able to dominate the manifest."""
    assert _MAX_PROBED_OPTIONS < _FULL_PROBED_OPTIONS <= 1000
    assert _MAX_DEP_PROBES < _FULL_DEP_PROBES <= 100
    assert _MAX_OPTION_PROBES <= _FULL_OPTION_PROBES <= 100


def test_dependency_probing_is_wider_under_full_traversal(tmp_path):
    """Each dependency act answers "which questions change when this one is
    answered?" — the half of the catalogue a rule-based negative case needs."""
    assert (_crawler(tmp_path, traversal=TRAVERSAL_FULL)._max_dep_probes
            > _crawler(tmp_path)._max_dep_probes)

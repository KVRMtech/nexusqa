"""WHAT THE CRAWL DID, BEFORE WHAT IT NEEDS — the copy a client actually reads.

The old diagnosis reported only the shortfall: "The crawl explored the app but
needs real values to go deeper: <15 names>. Provide values for: <15 names>. Then
re-crawl." Two things were wrong with that, and both were about honesty rather
than tone.

  1. It led with the ask, so a crawl that had answered thirteen of fifteen fields
     read as though it had achieved nothing. The thirteen is the larger and more
     decision-relevant number.
  2. It demanded a RE-CRAWL for a DATA gap. The pages were already catalogued —
     a missing value has nothing to do with discovery, and restarting discovery
     to apply one is the architectural mistake, not a UX detail.

These tests pin the shape of the replacement and, most importantly, that the
re-crawl demand cannot come back.
"""
from __future__ import annotations

from app.services import crawl_diagnosis as cd


def _f(name, **over):
    e = {"name": name, "filled": False, "semantic_type": ""}
    e.update(over)
    return e


def _diag(stats):
    return cd.diagnose(status="completed", error="", stats=stats)


def _productive(ledger, generated=4):
    return {"visits": 9, "generate": {"generated": generated},
            "coverage": {"field_ledger": ledger,
                         "fields_needing_seed": [e["name"] for e in ledger
                                                 if not e.get("filled")]}}


# ── lead with the work already done ─────────────────────────────────────────

def test_the_summary_states_what_was_populated_before_what_is_missing():
    """The example from the requirement: 15 discovered, 13 populated, 2 asked."""
    ledger = [_f(f"Field {i}", filled=True, provenance="synthesized")
              for i in range(13)]
    ledger += [_f("Policy Number"), _f("Member Number")]

    d = _diag(_productive(ledger))
    assert d["code"] == cd.CODE_COMPLETED_OK
    assert "Automatically populated 13 of 15 fields." in d["human"]
    assert "Policy Number" in d["human"] and "Member Number" in d["human"]


def test_the_evidence_carries_the_counts_and_the_provenance():
    """Autonomy decides how far it got; provenance decides what the green MEANS.
    Both travel in the evidence, so a reader can tell a run built on the client's
    own data from one built on invented data."""
    ledger = [_f("A", filled=True, provenance="provided"),
              _f("B", filled=True, provenance="synthesized"),
              _f("Policy Number")]
    ev = _diag(_productive(ledger))["evidence"]

    assert ev["auto_filled"] == 2
    assert ev["fill_provenance"] == {"provided": 1, "synthesized": 1}
    assert [a["name"] for a in ev["needs_assistance"]] == ["Policy Number"]


def test_a_crawl_that_needed_nobody_says_so():
    ledger = [_f(f"F{i}", filled=True, provenance="synthesized") for i in range(6)]
    assert "Nothing needs your input." in _diag(_productive(ledger))["human"]


# ── only genuine human requirements are asked for ───────────────────────────

def test_an_agent_gap_is_not_billed_to_the_client_as_a_requirement():
    """"Coverage preference" is a choice the agent should have been able to make.
    Listing it beside a policy number tells the client both are their problem."""
    ledger = [_f("A", filled=True, provenance="synthesized"),
              _f("Policy Number"), _f("Coverage preference")]
    d = _diag(_productive(ledger))

    assert d["fields"] == ["Policy Number"]
    assert [g["name"] for g in d["evidence"]["agent_gaps"]] == ["Coverage preference"]
    assert "our side" in d["human"].lower()


# ── THE ONE THAT MUST NOT REGRESS ───────────────────────────────────────────

def test_no_data_gap_message_ever_demands_a_re_crawl():
    """THE ARCHITECTURAL FIX, pinned as copy.

    A data gap must never force a discovery restart — the pages are already
    known. This walks the productive and non-productive paths and the
    ledger-absent fallback, because the demand only has to survive in one of them
    to be back in front of a client.
    """
    cases = [
        _productive([_f("A", filled=True, provenance="synthesized"),
                     _f("Policy Number")]),
        _productive([_f("Coverage preference")], generated=0),
        _productive([_f("A", filled=True, provenance="synthesized")]),
        # No field ledger at all — an older manifest.
        {"visits": 9, "generate": {"generated": 4},
         "coverage": {"fields_needing_seed": ["Payee"]}},
        {"visits": 9, "generate": {"generated": 0},
         "coverage": {"fields_needing_seed": ["Payee"]}},
    ]
    for stats in cases:
        d = _diag(stats)
        text = f"{d['human']} {d['remediation']}".lower()
        assert "re-crawl" not in text, d
        assert "re-run the crawl" not in text, d
        assert "crawl again" not in text, d


def test_the_residue_hint_survives_when_no_field_ledger_arrived():
    """REGRESSION GUARD. The richer classification depends on the per-field
    ledger; a crawl that recorded seeds without one must still surface them, or a
    value the operator could supply today silently stops being mentioned."""
    d = _diag({"visits": 9, "generate": {"generated": 4},
               "coverage": {"fields_needing_seed": ["Payee"]}})
    assert d["code"] == cd.CODE_COMPLETED_OK
    assert "Payee" in d["remediation"]
    assert d["fields"] == ["Payee"]


def test_the_non_productive_path_also_leads_with_what_was_populated():
    ledger = [_f("A", filled=True, provenance="synthesized"),
              _f("B", filled=True, provenance="journey"),
              _f("Policy Number")]
    d = _diag(_productive(ledger, generated=0))
    assert d["code"] == cd.CODE_SEEDS_NEEDED
    assert d["human"].startswith("Automatically populated 2 of 3 fields.")
    assert "already catalogued" in d["remediation"]

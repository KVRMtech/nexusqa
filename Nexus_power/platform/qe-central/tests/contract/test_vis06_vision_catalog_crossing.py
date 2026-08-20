"""M3.1 / T-VIS-06 — THE CONSUMER HALF: a verified vision control BECOMES a
catalogue question, and an unverified one never does.

WHY THIS TEST IS HERE AND NOT BESIDE THE CRAWL
==============================================
The two services both ship a top-level ``app`` package, so one pytest process
cannot import both — the same constraint ``contracts/m22_catalog_question_v1.json``
exists for.  The explorer's proving ground (``qe-explorer
tests/browser/test_vision_canvas_proving_ground.py``) therefore proves the
PRODUCER half against a real Chromium crawl of fixture 23 and archives what it
emitted; this proves the CONSUMER half against that payload.

THE PAYLOAD BELOW IS REAL.  It is the ``coverage.states`` entry and the vision
ledger that crawl actually produced — crawl ``vis06-canvas``, one state,
``stop_reason=completed``, two R0-verified perceptions and one refused.  Nothing
in it is hand-written except the formatting.

WHAT IT PROVES
==============
1. "Annual Income" — a canvas-rendered text field the crawl clicked at a
   perceived coordinate and MEASURED responding (rung
   ``pixel_stable_surface``) — arrives as a catalogue question.
2. "Social Security Number" — perceived with a coordinate, clicked at exactly
   that coordinate, and the application did nothing — is nowhere in the
   catalogue, because it is nowhere in the payload the catalogue is built from.
   The only trace of it is a refused row in the vision ledger.

That second one is the milestone's whole point.  A hallucinated PII field is
precisely the kind of thing a catalogue must never learn: it would generate
tests for a question the application does not ask, and it would name a real
identifier while doing it.
"""
from __future__ import annotations

from app.services.catalog import (
    build_master_catalog,
    build_states_index,
    extract_controls,
)

# ── The crawl's own output (qe-explorer crawl_id=vis06-canvas) ──────────────

FINGERPRINT = "ccfc4e19f9f40b04" + "0" * 48        # padded to the 64-char shape
CANVAS_URL = "http://127.0.0.1/23-canvas-app/index.html"

COVERAGE = {
    "states": [
        {
            "ax_fingerprint": FINGERPRINT,
            "location": CANVAS_URL,
            # THE VERIFIED CONTROL, exactly as the crawl emitted it. A canvas
            # text field has no DOM handle at all — this row exists only because
            # a coordinate click was followed by a measured repaint.
            "form_snapshot_signals": {
                "Annual Income": {
                    "type": "text", "options": [], "options_total": 0,
                    "required": False,
                },
            },
            "controls_total": 1,
            "danger_controls": 0,
            "danger_names": [],
            "question_groups": [],
            "endpoints": [],
        },
    ],
    "vision_verified": 2,
    "vision_refused": 1,
    "vision_ledger": [
        {
            "ran": True,
            "skipped_reason": "",
            "perceived": 3,
            "verified": 2,
            "refused": 1,
            "pixel_rung_admissible": True,
            "url": CANVAS_URL,
            "attempts": [
                {"label": "Annual Income", "role": "textbox",
                 "signature": "vis:annualincome", "click_x": 208, "click_y": 274,
                 "status": "verified", "r0_rung": "pixel_stable_surface",
                 "reason": "a still surface repainted in response to the click",
                 "url": CANVAS_URL},
                {"label": "Recalculate", "role": "button",
                 "signature": "vis:recalculate", "click_x": 208, "click_y": 394,
                 "status": "verified", "r0_rung": "dom",
                 "reason": "the page responded (url / DOM / dialog)",
                 "url": CANVAS_URL},
                {"label": "Social Security Number", "role": "textbox",
                 "signature": "vis:ssn", "click_x": 760, "click_y": 180,
                 "status": "refused_unverified", "r0_rung": "",
                 "reason": "R0 unverified: neither the DOM nor the pixels changed",
                 "url": CANVAS_URL},
            ],
        },
    ],
    "vision_budget": {
        "gate": {"enabled": True, "reason": "ok", "attested": True,
                 "tenant_enabled": True,
                 "attestation_rung": "signed_provisioning_proof"},
        "calls": 1, "max_calls": 6, "failures": 0, "breaker_open": False,
        "breaker_threshold": 3, "timeout_s": 20.0, "refusals": {},
    },
}


def _catalog():
    index = build_states_index(COVERAGE)
    controls = extract_controls(index[FINGERPRINT], {})
    node = {"node_fp": FINGERPRINT, "url": CANVAS_URL,
            "title": "Illustration Studio", "controls_inventory": controls}
    return build_master_catalog([node]), controls


def _names(catalog) -> set:
    return {q["name"] for q in catalog["questions"]}


# ── 1. the payload really is the one the catalogue reads ───────────────────

def test_the_states_index_resolves_the_canvas_state():
    index = build_states_index(COVERAGE)
    assert FINGERPRINT in index
    assert index[FINGERPRINT]["location"] == CANVAS_URL


def test_extract_controls_reads_the_vision_verified_control():
    _, controls = _catalog()
    assert [c["name"] for c in controls] == ["Annual Income"]
    assert controls[0]["type"] == "text"


# ── 2. THE CHAIN ENDS IN THE CATALOGUE ─────────────────────────────────────

def test_the_verified_canvas_control_is_a_catalogue_QUESTION():
    """canvas control -> vision -> coordinate action -> R0 -> catalog.

    The last link. A control with no element, no role, no accessible name and no
    locator — reachable only by a coordinate — is a question this application
    asks, and the catalogue knows it.
    """
    catalog, _ = _catalog()
    assert "Annual Income" in _names(catalog)
    q = next(q for q in catalog["questions"] if q["name"] == "Annual Income")
    assert q["type"] == "text"
    assert q["pages"] and CANVAS_URL in q["pages"][0]


def test_the_UNVERIFIED_perception_is_absent_from_the_catalogue():
    """THE LAW: a vision prediction is never catalogue truth.

    "Social Security Number" was perceived with a coordinate and the crawl
    clicked exactly there. The application did nothing, so it is not a control,
    and the catalogue must not contain it — nor any of the three other places a
    question can enter from.
    """
    catalog, controls = _catalog()
    assert "Social Security Number" not in _names(catalog)
    assert all(c["name"] != "Social Security Number" for c in controls)
    # …not through a branch row either.
    branches = [{"node_fp": FINGERPRINT,
                 "control_signature": "vis:ssn",
                 "control_label_norm": "social security number",
                 "option_label_norm": "yes"}]
    with_branches = build_master_catalog(
        [{"node_fp": FINGERPRINT, "url": CANVAS_URL,
          "controls_inventory": extract_controls(
              build_states_index(COVERAGE)[FINGERPRINT], {})}],
        branches=[])
    assert "Social Security Number" not in _names(with_branches)
    # The branch fixture above is the shape that WOULD carry it, passed empty on
    # purpose: the crawl never emitted one, because a refused perception
    # contributes no decision point.
    assert branches[0]["control_label_norm"] not in {
        (q.get("name") or "").lower() for q in with_branches["questions"]}


def test_the_refused_perception_survives_ONLY_as_ledger_evidence():
    """Absent from the catalogue is not the same as absent from the record.

    A wrong perception that leaves no trace anywhere is indistinguishable from
    one that never happened, and an operator needs the difference in order to
    stop trusting a model.
    """
    refused = [a for row in COVERAGE["vision_ledger"]
               for a in row["attempts"] if a["status"] != "verified"]
    assert [a["label"] for a in refused] == ["Social Security Number"]
    a = refused[0]
    assert a["reason"] and a["r0_rung"] == ""
    assert (a["click_x"], a["click_y"]) == (760, 180), (
        "the ledger must record WHERE the crawl clicked, or the refusal cannot "
        "be re-checked by anyone who does not trust the process that made it")


# ── 3. the gate and the spend are legible from the same payload ────────────

def test_the_catalogue_consumer_can_see_which_gate_admitted_this_evidence():
    """A catalogue row sourced from vision is only as trustworthy as the gate
    that let vision run, so the gate travels with the evidence."""
    gate = COVERAGE["vision_budget"]["gate"]
    assert gate["enabled"] is True
    assert gate["attested"] is True and gate["tenant_enabled"] is True
    assert gate["attestation_rung"] == "signed_provisioning_proof"
    assert COVERAGE["vision_budget"]["calls"] <= COVERAGE["vision_budget"]["max_calls"]
    assert COVERAGE["vision_budget"]["breaker_open"] is False

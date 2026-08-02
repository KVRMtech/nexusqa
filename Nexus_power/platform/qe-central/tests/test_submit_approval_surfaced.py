"""The submit the crawl found must be APPROVABLE in the product.

A client targeted /portal/claims/new three times. Every crawl recorded
coverage.submit_candidates = ["Submit claim"] and forms_submitted = 0, because the
crawler only presses a submit whose name is in fences.submit_approvals.

The ONLY way to populate that list is the Seed Manifest checkbox
"Allow the crawl to submit ...", which the portal renders exclusively for items with
disposition APPROVE. The manifest never produced one: the seed-manifest route read
fields_needing_seed, opaque_surfaces and unhandled_controls out of the crawl's
coverage and ignored submit_candidates, so the submit never reached the classifier.

The product asked for an approval it gave no way to grant.
"""
from datetime import date

from app.services.dispositions import APPROVE, FieldSignal, classify_manifest


def _fields():
    return [
        FieldSignal(label="Policy", type="select", options=("P-1",)),
        FieldSignal(label="Claim type", type="select", options=("Auto",)),
        FieldSignal(label="Claimant name", type="text"),
        FieldSignal(label="Date of loss", type="date"),
        FieldSignal(label="Additional details", type="text"),
    ]


def test_without_the_submit_no_approval_row_exists():
    """The state the client was stuck in — reproduced."""
    m = classify_manifest(_fields(), today=date(2026, 8, 1))
    assert not [i for i in m["full"] if i["disposition"] == APPROVE]
    assert m["counts"][APPROVE] == 0


def test_a_submit_candidate_becomes_an_APPROVE_row():
    fields = _fields() + [FieldSignal(label="Submit claim", type="submit")]
    m = classify_manifest(fields, submit_labels=["Submit claim"], today=date(2026, 8, 1))
    approvals = [i for i in m["full"] if i["disposition"] == APPROVE]
    assert [i["label"] for i in approvals] == ["Submit claim"]
    # and it must reach `recommended`, which is what the panel renders
    assert "Submit claim" in [i["label"] for i in m["recommended"]]


def test_the_submit_label_alone_is_enough_even_without_a_type():
    """The crawl's coverage gives a NAME, not a control type."""
    fields = _fields() + [FieldSignal(label="Submit claim")]
    m = classify_manifest(fields, submit_labels=["Submit claim"], today=date(2026, 8, 1))
    assert [i["label"] for i in m["full"] if i["disposition"] == APPROVE] == ["Submit claim"]


def test_an_approval_is_never_prefilled_as_a_value():
    """APPROVE is a decision, not a value — it must not leak into the crawl's fill."""
    fields = _fields() + [FieldSignal(label="Submit claim", type="submit")]
    m = classify_manifest(fields, submit_labels=["Submit claim"], today=date(2026, 8, 1))
    assert "Submit claim" not in (m["prefill"] or {})


# ── the wiring: coverage -> manifest -> classifier ───────────────────────────

def test_the_route_reads_submit_candidates_out_of_the_crawl_coverage():
    src = open("app/routers/apps.py", encoding="utf-8").read()
    assert '_cov.get("submit_candidates")' in src
    assert "submit_candidates=submit_candidates," in src


def test_the_manifest_passes_them_to_the_classifier():
    src = open("app/services/seed_manifest.py", encoding="utf-8").read()
    assert "submit_candidates: Iterable[str] = ()" in src
    assert "submit_labels=submit_labels," in src
    # …and adds the control as a signal so it appears even when the field inventory
    # (which holds value fields) never saw it
    assert 'FieldSignal(label=lbl, type="submit")' in src


def test_a_duplicate_submit_is_not_added_twice():
    src = open("app/services/seed_manifest.py", encoding="utf-8").read()
    assert "if normalize_label(lbl) not in have:" in src

"""Client-friendly, actionable translations of the enumerated substrate refusals.

The substrate REFUSES a dishonest or broken capture rather than green-wash it — an
honest, first-class outcome (:data:`app.substrate.schema.REFUSAL_REASONS`).  But the
raw reason string (``missing_fill_value: action #3 (type 'Product') carries no
committed value``) reads like a stack trace to an operator watching a crawl.

This maps each enumerated reason CODE to a plain-English explanation plus the concrete
next step.  It never HIDES that something needs attention — the honest signal is fully
preserved (the technical reason still rides in the API ``reason`` field, the row
``stats`` and the logs); only the phrasing the operator READS is humane.

Kept in lock-step with ``REFUSAL_REASONS`` — :func:`test_every_reason_has_a_message`
fails CI if a new reason is added without a message here.
"""
from __future__ import annotations

#: reason-code → one plain-English, actionable sentence (what happened; what to do).
#: A capture "hiccup" phrasing is used where a re-crawl is the honest fix; a genuine
#: data/config gap tells the operator the specific thing to provide.
_MESSAGES: dict[str, str] = {
    "missing_fill_value": (
        "While exploring your app we reached a form field (often a dropdown) but "
        "couldn't record a value for it, so we can't build a reliable test from that "
        "step. Re-crawl to try again — this usually resolves it. If it keeps "
        "happening, that field needs a specific value you can provide in the app's "
        "data settings."
    ),
    "missing_after_bundle": (
        "A recorded step is missing its result, so we can't prove what it did. "
        "Re-crawl the app to capture a complete run."
    ),
    "missing_target_label": (
        "A recorded step pointed at an on-screen element we couldn't name. Re-crawl "
        "the app; if it persists, that element may be non-standard and need a look."
    ),
    "invalid_verb": (
        "The crawl recorded an interaction we don't yet turn into a test step. "
        "Re-crawl the app; if it persists, let us know the app so we can add support."
    ),
    "invalid_target_kind": (
        "The crawl recorded an element type we don't yet support. Re-crawl the app; "
        "if it persists, let us know the app so we can add support."
    ),
    "non_monotonic_sequence_index": (
        "The recorded steps arrived out of order (a capture hiccup). Re-crawl the app."
    ),
    "non_monotonic_subaction_index": (
        "The recorded steps on a page arrived out of order (a capture hiccup). "
        "Re-crawl the app."
    ),
    "non_monotonic_timeline": (
        "The recorded steps' timing was inconsistent (a capture hiccup). Re-crawl the app."
    ),
    "invalid_location": (
        "A recorded page location was malformed (a capture hiccup). Re-crawl the app."
    ),
    "invalid_visit_window": (
        "A page-visit's timing was inconsistent (a capture hiccup). Re-crawl the app."
    ),
    "screenshot_outside_visit_window": (
        "A screenshot's timing didn't line up with the page it belongs to (a capture "
        "hiccup). Re-crawl the app."
    ),
    "screenshot_missing_data": (
        "A screenshot from the crawl couldn't be read. Re-crawl the app to recapture it."
    ),
    "duplicate_frame_index": (
        "Two screenshots claimed the same slot (a capture hiccup). Re-crawl the app."
    ),
    "frame_count_mismatch": (
        "The screenshot count didn't match what was captured (a capture hiccup). "
        "Re-crawl the app."
    ),
    "invalid_crawl_id": (
        "This crawl's internal identifier was malformed. Please start a fresh crawl."
    ),
    "invalid_target_url": (
        "The app's URL wasn't accepted. Check the URL in the app's settings and re-crawl."
    ),
    "invalid_extractor_version": (
        "An internal version tag was malformed. Please start a fresh crawl."
    ),
    "duplicate_extractor_version": (
        "This exact crawl was already processed. Start a fresh crawl to capture the "
        "latest version of the app."
    ),
    "schema_invalid": (
        "The crawl produced data we couldn't validate into tests. Re-crawl the app; "
        "if it persists, contact support with the app details."
    ),
}

#: Safe, still-actionable fallback for an unrecognised code (should never be hit in
#: practice — the completeness test guarantees every enumerated reason is mapped).
_DEFAULT = (
    "We couldn't build tests from this crawl because the captured data didn't pass "
    "our honesty checks. Re-crawl the app; if it persists, contact support."
)


def client_refusal_message(reason_code: str) -> str:
    """Return a plain-English, actionable message for an enumerated refusal code.

    ``reason_code`` is :attr:`app.substrate.schema.RefusalError.reason` (one of
    ``REFUSAL_REASONS``).  An unknown code returns a safe, generic actionable
    message — never a blank string and never the raw code.
    """
    return _MESSAGES.get((reason_code or "").strip(), _DEFAULT)

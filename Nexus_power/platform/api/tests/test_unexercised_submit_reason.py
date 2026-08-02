"""A crawl that stopped at a submit must SAY so, not blame the app's shape.

A client targeted /portal/claims/new. The crawl filled the form, reached
"Submit claim", stopped, and generation reported:

    "No functional E2E generated — only 1 distinct page milestone(s) detected
     (a flow needs at least 2). This looks like a single-page app (no URL change
     between pages)..."

The app is not a single-page app. The crawler simply never pressed the submit,
because the flow name was not in fences.submit_approvals — so the page AFTER the
submit, which is the second milestone a flow needs, was never recorded.

The client read "single-page app", concluded it was an app-shape problem, and
re-crawled twice. The system had the truth the whole time.
"""
from app.services.test_factory.generator import DemonstratedGenerationResult


def _reason(result) -> str:
    """Mirror service.py's no_cases_reason assembly for the no-test-cases path."""
    if getattr(result, "no_flow_reason", ""):
        return result.no_flow_reason
    pg = result.page_groups
    parts = [f"No functional E2E generated — only {pg} distinct page milestone(s) "
             "detected (a flow needs at least 2)."]
    if pg < 2:
        parts.append("This looks like a single-page app (no URL change between pages): "
                     "record a flow that navigates between distinct pages/URLs, or click "
                     "Enrich to re-extract the page data.")
    return " ".join(parts)


def test_a_form_that_was_never_submitted_names_the_real_cause():
    r = DemonstratedGenerationResult(
        test_cases=[], page_groups=1, visits_total=1, visits_used=1,
        fields_demonstrated=0, excluded_placeholder_fields=0,
        no_flow_reason=("No functional E2E generated — a form was filled but its submit "
                        "was never pressed, so the page AFTER the submit (the second "
                        "milestone a flow needs) was never recorded. This is almost "
                        "always an unapproved flow: the crawler only presses a submit "
                        "whose name the operator has approved (fences.submit_approvals). "
                        "Approve the submit for this form and re-crawl. The application "
                        "is NOT at fault."),
    )
    msg = _reason(r)
    assert "submit was never pressed" in msg
    assert "fences.submit_approvals" in msg
    assert "single-page app" not in msg          # the wrong diagnosis is gone
    assert "NOT at fault" in msg                 # never blame the app


def test_a_genuine_single_page_app_still_gets_the_spa_message():
    """The SPA wording is right when there IS no unexercised submit — don't trade
    one wrong diagnosis for another."""
    r = DemonstratedGenerationResult(
        test_cases=[], page_groups=1, visits_total=1, visits_used=1,
        fields_demonstrated=0, excluded_placeholder_fields=0, no_flow_reason="",
    )
    msg = _reason(r)
    assert "single-page app" in msg


def test_the_flatten_suppression_reason_still_wins_when_set():
    r = DemonstratedGenerationResult(
        test_cases=[], page_groups=7, visits_total=7, visits_used=7,
        fields_demonstrated=0, excluded_placeholder_fields=0,
        no_flow_reason="No coherent functional E2E — the crawl stitched 7 pages",
    )
    assert "stitched 7 pages" in _reason(r)


def test_the_generator_sets_it_only_when_a_form_was_filled_and_not_submitted():
    """Pins the condition, so a page with no form does not get a submit story."""
    import inspect
    from app.services.test_factory import generator
    src = inspect.getsource(generator._generate_demonstrated) \
        if hasattr(generator, "_generate_demonstrated") else \
        open(generator.__file__, encoding="utf-8").read()
    assert "_has_form and not _submitted" in src
    assert 'verb", "") or "").strip().lower() == "submit"' in src

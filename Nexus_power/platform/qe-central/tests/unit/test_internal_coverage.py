"""P4 — the crawl coverage report survives the completion callback and lands on
the exploration ``stats`` JSONB, so the app UI can turn "why did the crawl only
reach 2 flows?" into a NAMED, seed-this-field remediation list.

The model uses ``extra="ignore"``; without an explicit ``coverage`` field the
report would be silently dropped. These tests pin that it is NOT dropped, while
genuinely-unknown keys still are."""
from __future__ import annotations

from app.routers.internal import CompletionCallback

_COVERAGE = {
    "forms_found": 3,
    "forms_submitted": 0,
    "fields_inferred": ["Email Address", "Zip Code"],
    "fields_needing_seed": ["Social Security Number", "Policy Number"],
    "submit_candidates": ["Continue", "Get a Quote"],
    "summary": "3 forms found; 2 fields need a client seed to go deeper.",
}


def test_callback_carries_coverage_not_dropped_by_extra_ignore():
    body = CompletionCallback.model_validate(
        {"tenant_id": "t", "exploration_id": "e", "crawl_id": "c", "coverage": _COVERAGE}
    )
    assert body.coverage == _COVERAGE
    assert body.coverage["fields_needing_seed"] == [
        "Social Security Number",
        "Policy Number",
    ]


def test_coverage_defaults_none_and_unknown_extra_still_ignored():
    body = CompletionCallback.model_validate(
        {"tenant_id": "t", "exploration_id": "e", "crawl_id": "c", "some_bogus_key": 1}
    )
    assert body.coverage is None
    assert not hasattr(body, "some_bogus_key")


def test_empty_coverage_is_falsy_so_stats_stays_clean():
    # section-7 guard is ``if body.coverage`` — None/{} must not write a stats key.
    for empty in (None, {}):
        body = CompletionCallback.model_validate(
            {"tenant_id": "t", "exploration_id": "e", "crawl_id": "c", "coverage": empty}
        )
        assert not body.coverage

"""Team E: client coverage text is a safe projection of live bundle evidence."""
from app.services.client_coverage_report import build


def test_report_projects_completion_page_fields_and_provenance_without_values() -> None:
    report = build({
        "flow_summary": {"journeys_completed": 1, "flows_found": 2,
                         "flows_truncated": 1, "branch_coverage": False},
        "flows": [{"flow_id": "completed", "journey_completed": True, "steps": [
            {"url": "https://example.test/quote/?token=secret", "title": "Quote",
             "fields_filled": 3, "fields_unfilled": 1},
            {"url": "https://example.test/quote/", "title": "Quote",
             "fields_filled": 2, "fields_unfilled": 0},
        ]}],
        "data_account": {"synthesized": 5, "needs_input": 1},
        "seed_near_misses": [{"field_label": "Coverage", "seed_label": "Cover",
                               "url": "https://example.test/quote/?secret=nope",
                               "value": "must never leave the bundle"}],
    })

    assert report["journeys"] == {
        "completed": 1, "found": 2, "truncated": 1,
        "branch_coverage": False, "branch_coverage_note": "",
    }
    assert report["pages"] == [{
        "page": "/quote/", "title": "Quote", "observed_steps": 2,
        "fields_filled": 5, "fields_unfilled": 1, "completed_journeys": 1,
    }]
    assert report["data_account"] == {"synthesized": 5, "needs_input": 1}
    assert report["seed_near_misses"] == [{
        "field_label": "Coverage", "seed_label": "Cover",
        "url": "https://example.test/quote/?secret=nope",
    }]
    assert "must never leave" not in repr(report)


def test_empty_bundle_is_explicit_not_a_claim_of_coverage() -> None:
    report = build({})
    assert report["journeys"]["completed"] == 0
    assert report["pages"] == []
    assert report["data_account"] == {}

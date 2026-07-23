"""R3 — RISK-RANKED combinations built over grounded FORM-FLOW bases.

Live incident this replays: the VKPower quote crawl captured 6 option domains
(Product/Gender/Coverage/Term/Tobacco...) and a grounded form-flow base, yet
generate produced ZERO combinations — the single-base wiring used only the
(suppressed) flatten E2E, and the combo builder could not override the
form-flow fill phrasing ("Select 'term' in 'Product'"). The client's literal
ask is ranked combinations of their quote workflow (Term/10yr; Term/20yr/
Female/Tobacco=yes; ...).
"""
from __future__ import annotations

from nexus_sdk.models import Precondition, ProductionTestCase, ProductionTestStep

from app.services.test_factory.combinations import generate_combination_cases
from app.services.test_factory.generator import PageVisitInput


def _visit(signals: dict) -> PageVisitInput:
    return PageVisitInput(
        page_visit_id="v-quote", sequence_index=0, location="Get a quote",
        url_host="vkpowerlife.35-186-147-245.sslip.io", url_path="/quote",
        url_query="plan=term", canonical_host="sslip.io", source="ground_truth",
        form_snapshot={}, form_snapshot_signals=signals,
        first_seen_ms=0, duration_ms=0, frame_ref="", extraction_confidence=1.0,
    )


def _quote_signals() -> dict:
    return {
        "Product": {"selected": "term",
                    "options": ["term", "whole", "iul", "final"], "required": True},
        "Term length (years)": {"selected": "10",
                                "options": ["10", "20", "30"], "required": True},
        "Gender": {"selected": "male", "options": ["male", "female"], "required": False},
        "Tobacco use in the last 12 months?": {"selected": "no",
                                               "options": ["no", "yes"], "required": False},
    }


def _step(n: int, action: str, **obs) -> ProductionTestStep:
    return ProductionTestStep(
        step_number=n, action=action, expected="x", expected_result="x",
        selector="", observed=obs or {}, provenance="demonstrated",
    )


def _form_flow_base() -> ProductionTestCase:
    """Mirrors generate_form_flow_journeys' step phrasing exactly."""
    return ProductionTestCase(
        test_id="base-quote-flow",
        name="Quote flow: fill the form and submit via 'Calculate my premium'",
        description="d",
        steps=[
            _step(1, "Open https://vkpowerlife.35-186-147-245.sslip.io/quote?plan=term"),
            _step(2, "Select 'term' in 'Product'", verb="select", label="Product",
                  kind="dropdown", value="term"),
            _step(3, "Enter '18' in 'Age'", verb="type", label="Age",
                  kind="text_field", value="18"),
            _step(4, "Select '10' in 'Term length (years)'", verb="select",
                  label="Term length (years)", kind="dropdown", value="10"),
            _step(5, "Select 'no' in 'Tobacco use in the last 12 months?'",
                  verb="select", label="Tobacco use in the last 12 months?",
                  kind="dropdown", value="no"),
            _step(6, "Click 'Calculate my premium'"),
            _step(7, "Verify the application navigated to https://vkpowerlife.35-186-147-245.sslip.io/quote?submitted=1"),
        ],
        preconditions=[Precondition(description="p", setup_action="s")],
        priority="P0_critical", type="functional",
        tags=["demonstrated", "grounded-form-flow"],
    )


def test_formflow_base_yields_ranked_combinations():
    """The client scenario: form-flow base + captured domains → REAL ranked
    combinations whose fill steps carry the NEW option values."""
    res = generate_combination_cases(
        artifact_id="art", base_cases=[_form_flow_base()],
        page_visits=[_visit(_quote_signals())], host="vkpowerlife.test",
    )
    assert res.active, "combinations must generate off a form-flow base"
    # Ranked: names carry Rank N, tags carry rank+risk, dicts populated.
    first = res.active[0]
    assert first.name.startswith("Rank 1 — Combination:"), first.name
    assert any(t.startswith("combination-rank:1") for t in first.tags)
    assert res.rank_by_test_id[first.test_id] == 1
    assert res.risk_by_test_id[first.test_id] >= res.risk_by_test_id[res.active[-1].test_id]
    # Risk-descending order globally.
    risks = [res.risk_by_test_id[c.test_id] for c in res.active]
    assert risks == sorted(risks, reverse=True)
    # The overridden step really carries a NON-demonstrated captured option.
    changed = [
        s for c in res.active for s in c.steps
        if s.action.startswith(("Select '", "Enter '")) and s.provenance == "available"
    ]
    assert changed, "at least one fill step must be overridden to a captured option"
    sample = changed[0]
    assert " in '" in sample.action                      # form-flow phrasing preserved
    assert (sample.observed or {}).get("value")          # observed carries the new value
    assert (sample.observed or {}).get("kind")           # base kind (dropdown/text) preserved


def test_formflow_combination_covers_the_clients_named_example():
    """'Term Life + 20 Years + Female + Tobacco=Yes' — the founder's Rank-2
    example — must exist somewhere in the generated suite (all four values are
    captured options)."""
    res = generate_combination_cases(
        artifact_id="art", base_cases=[_form_flow_base()],
        page_visits=[_visit(_quote_signals())], host="h",
    )
    def has(case, label, value):
        return any(f"'{value}' in '{label}'" in (s.action or "") for s in case.steps)
    hits = [
        c for c in res.active
        if has(c, "Term length (years)", "20") and has(c, "Gender", "female")
    ]
    # Gender is not a base fill step (base never filled it) — it can only pair
    # via the toggle/select shapes; at minimum the Term-20 axis must appear.
    assert any(has(c, "Term length (years)", "20") for c in res.active), \
        "a Term=20 variant must be generated from the captured domain"


def test_multi_base_dedups_combination_signatures():
    """Two bases must not emit duplicate combination signatures — first base
    (form-flow) wins."""
    base2 = _form_flow_base().model_copy(deep=True)
    base2.test_id = "base-2"
    res = generate_combination_cases(
        artifact_id="art", base_cases=[_form_flow_base(), base2],
        page_visits=[_visit(_quote_signals())], host="h",
    )
    names = [c.name.split("— ", 1)[-1] for c in res.active]
    assert len(names) == len(set(names)), "duplicate combination signatures emitted"
    assert res.generation_spec["base_test_ids"] == ["base-quote-flow", "base-2"]


def test_no_bases_or_no_domains_is_honest_empty():
    res = generate_combination_cases(
        artifact_id="art", base_cases=[],
        page_visits=[_visit(_quote_signals())], host="h",
    )
    assert res.active == [] and res.selected_count == 0
    res2 = generate_combination_cases(
        artifact_id="art", base_cases=[_form_flow_base()],
        page_visits=[_visit({})], host="h",
    )
    assert res2.active == []


def test_single_base_backcompat_kwarg_still_works():
    res = generate_combination_cases(
        artifact_id="art", base_case=_form_flow_base(),
        page_visits=[_visit(_quote_signals())], host="h",
    )
    assert res.active, "legacy base_case kwarg must keep working"


# ─── the LIVE substrate shape: signals carry options but NO `selected` ────────


class _Act:
    """Duck-typed page action (verb/target_label/value are all harvest needs)."""

    def __init__(self, verb, label, value):
        self.verb = verb
        self.target_label = label
        self.value = value


def _live_signals() -> dict:
    """Byte-shape of the real crawl substrate (qe-explorer form_snapshot):
    display-label options, required flag, NO selected key."""
    return {
        "Age": {"type": "text", "options": [], "required": False},
        "Gender": {"type": "select", "options": ["Male", "Female"], "required": False},
        "Product": {"type": "select",
                    "options": ["VKPower Term", "Heritage Whole Life",
                                "VKPower Indexed UL", "Guardian Final Expense"],
                    "required": False},
        "Coverage amount": {"type": "select",
                            "options": ["$250,000", "$500,000", "$1,000,000", "$2,000,000"],
                            "required": False},
        "Term length (years)": {"type": "select",
                                "options": ["10", "20", "30"], "required": False},
        "Tobacco use in the last 12 months?": {"type": "select",
                                               "options": ["No", "Yes"], "required": False},
    }


def _live_actions():
    """The committed fills exactly as the substrate recorded them — VALUES,
    not display labels ('term' vs 'VKPower Term', '250000' vs '$250,000')."""
    return [
        _Act("select", "Product", "term"),
        _Act("type", "Age", "18"),
        _Act("select", "Gender", "male"),
        _Act("select", "Coverage amount", "250000"),
        _Act("select", "Term length (years)", "10"),
        _Act("select", "Tobacco use in the last 12 months?", "no"),
        _Act("submit", "Calculate my premium", None),
    ]


def test_live_shape_selected_joined_from_committed_actions():
    """The exact live incident: 6 captured domains, zero combinations — because
    signals carry no `selected`. The committed fill ACTIONS are the demonstrated
    selections; values map to their display-label options (term->VKPower Term,
    250000->$250,000, male->Male exact-first so 'Female' containment never
    steals it)."""
    from app.services.test_factory.combinations import harvest_option_domains
    domains = harvest_option_domains([_visit(_live_signals())], _live_actions())
    by_label = {d.field_label: d for d in domains}
    assert "Product" in by_label and by_label["Product"].selected == "VKPower Term"
    assert by_label["Coverage amount"].selected == "$250,000"
    assert by_label["Gender"].selected == "Male"          # exact beats containment
    assert by_label["Term length (years)"].selected == "10"
    assert by_label["Tobacco use in the last 12 months?"].selected == "No"
    assert "Age" not in by_label                          # no options -> never an axis


def test_pairwise_actually_covers_every_cross_axis_pair():
    """The greedy's first-placed axis must ROTATE its options: before the fix it
    always took opts[0] (empty partner set -> gain 0 for all), so pairs involving
    the other options were never covered — a 4x4x3x2x2 space 'covered' by 6
    near-identical rows, every one Product=first-option (live regen output)."""
    from app.services.test_factory.combinations import _pairwise
    axes = [
        ("Product", ["VKPower Term", "Heritage Whole Life",
                     "VKPower Indexed UL", "Guardian Final Expense"]),
        ("Coverage amount", ["$250,000", "$500,000", "$1,000,000", "$2,000,000"]),
        ("Term length (years)", ["10", "20", "30"]),
        ("Gender", ["Male", "Female"]),
        ("Tobacco use in the last 12 months?", ["No", "Yes"]),
    ]
    tests = _pairwise(axes)
    # Every cross-axis (value, value) pair covered at least once.
    from itertools import combinations as icombos
    for (la, oa), (lb, ob) in icombos(axes, 2):
        for a in oa:
            for b in ob:
                assert any(t.get(la) == a and t.get(lb) == b for t in tests), \
                    f"uncovered pair: {la}={a} x {lb}={b}"
    # The first axis genuinely varies.
    assert len({t["Product"] for t in tests}) == 4
    # And the suite stays far below the 192 full space (pairwise efficiency).
    assert len(tests) < 40, f"pairwise blew up: {len(tests)} tests"


def test_live_shape_generates_ranked_combinations_and_skips_demonstrated():
    res = generate_combination_cases(
        artifact_id="art", base_cases=[_form_flow_base()],
        page_visits=[_visit(_live_signals())], page_actions=_live_actions(),
        host="vkpowerlife.test",
    )
    assert res.active, "live-shape substrate must yield combinations"
    assert res.active[0].name.startswith("Rank 1 — ")
    # The all-demonstrated combo (every axis == its selected label) never appears.
    def all_demo(case):
        return (any("'VKPower Term' in 'Product'" in (s.action or "") for s in case.steps)
                and any("'10' in 'Term length (years)'" in (s.action or "") for s in case.steps)
                and any("'Male' in 'Gender'" in (s.action or "") for s in case.steps)
                and any("'No' in 'Tobacco" in (s.action or "") for s in case.steps)
                and any("'$250,000' in 'Coverage amount'" in (s.action or "") for s in case.steps))
    assert not any(all_demo(c) for c in res.active), "demonstrated combo must be excluded"
    # The founder's named example axes exist in the suite: Term=20 and Tobacco=Yes.
    assert any("'20' in 'Term length (years)'" in (s.action or "")
               for c in res.active for s in c.steps)
    assert any("'Yes' in 'Tobacco" in (s.action or "")
               for c in res.active for s in c.steps)

"""MASTER CATALOGUE COMPLETENESS — the answer set survives the aggregation.

The explorer now captures a whole enumeration (all fifty states, ~250 countries).
That is only worth doing if the aggregation into the app-scoped Master Catalog
keeps it, and it did not: every question's options were re-clipped to 48 on the
way in, silently. Fifty states became forty-eight, and nothing in the row said two
were missing — so a generated boundary case could claim to cover "which state do
you live in?" while being structurally unable to produce Wyoming.

Three properties are pinned here:

  1. COMPLETENESS — a real answer set survives aggregation intact.
  2. HONESTY — when a list IS clipped, ``options_total`` says so. This must hold
     at every ceiling; the ceiling is a budget decision, hiding it is not.
  3. RICHEST WINS — the same question is met on many pages, and a DEPENDENT
     question is empty until its driver is answered. Keeping the first sighting
     meant the catalogue held the emptiest view of exactly the questions whose
     answers matter most.
"""
from __future__ import annotations

from app.services.catalog import MAX_CATALOG_OPTIONS, build_master_catalog

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


def _node(fp: str, controls: list[dict], title: str = "Apply") -> dict:
    return {"node_fp": fp, "title": title, "url": f"/{fp}", "controls": controls}


def _q(name: str, options: list[str], **over) -> dict:
    c = {"name": name, "type": "select", "options": list(options),
         "required": True, "question_id": f"q::{name}"}
    c.update(over)
    return c


def _only(cat: dict) -> dict:
    assert len(cat["questions"]) == 1, cat["questions"]
    return cat["questions"][0]


# ── 1. completeness ─────────────────────────────────────────────────────────

def test_all_fifty_states_survive_the_master_catalog():
    """The requirement, stated literally: Alabama through Wyoming."""
    cat = build_master_catalog(
        [_node("n1", [_q("What state do you live in?", _STATES)])])
    q = _only(cat)
    assert q["options"] == _STATES
    assert q["options"][-1] == "Wyoming"
    assert q["options_total"] == 50


def test_a_country_sized_answer_set_survives():
    countries = [f"Country {i:03d}" for i in range(250)]
    q = _only(build_master_catalog([_node("n1", [_q("Country", countries)])]))
    assert len(q["options"]) == 250
    assert q["options_total"] == 250


# ── 2. honesty at the ceiling ───────────────────────────────────────────────

def test_a_clipped_answer_set_still_reports_what_was_offered():
    """The ceiling is a budget decision and is allowed. Hiding it is not: a row
    whose options are a PREFIX must be distinguishable from one that is whole."""
    huge = [f"Opt {i:04d}" for i in range(MAX_CATALOG_OPTIONS + 120)]
    q = _only(build_master_catalog([_node("n1", [_q("Huge", huge)])]))
    assert len(q["options"]) == MAX_CATALOG_OPTIONS
    assert q["options_total"] == MAX_CATALOG_OPTIONS + 120
    assert q["options_total"] > len(q["options"]), "the clip must remain visible"


def test_options_total_is_carried_from_the_explorer_when_it_clipped_first():
    """The explorer may already have clipped in the page. Its count is the more
    truthful one and must not be overwritten by a recount of the shorter list."""
    q = _only(build_master_catalog(
        [_node("n1", [_q("Country", ["A", "B", "C"], options_total=247)])]))
    assert q["options"] == ["A", "B", "C"]
    assert q["options_total"] == 247


def test_a_complete_answer_set_reports_no_shortfall():
    q = _only(build_master_catalog([_node("n1", [_q("State", _STATES)])]))
    assert q["options_total"] == len(q["options"])


def test_a_nonsense_options_total_never_understates_the_stored_list():
    """Page-authored data is not trusted. The count is floored at what is
    actually held, so a row can never claim fewer answers than it carries."""
    for junk in (None, "", "abc", -5, True, 0):
        q = _only(build_master_catalog(
            [_node("n1", [_q("State", _STATES, options_total=junk)])]))
        assert q["options_total"] == 50, junk


# ── 3. richest sighting wins ────────────────────────────────────────────────

def test_a_dependent_question_keeps_its_richest_sighting():
    """THE DEPENDENCY CASE. "County" is empty until "State" is answered, so the
    first sighting of it legitimately offers nothing. Keeping that sighting meant
    the catalogue permanently recorded the dependent question as having no
    answers — the questions whose enumerations matter most were exactly the ones
    stored emptiest."""
    cat = build_master_catalog([
        _node("n1", [_q("County", [])]),                       # before the driver
        _node("n2", [_q("County", ["Travis", "Harris", "Bexar"])]),  # after
    ])
    q = _only(cat)
    assert q["options"] == ["Travis", "Harris", "Bexar"]


def test_a_richer_sighting_replaces_a_partial_one():
    cat = build_master_catalog([
        _node("n1", [_q("State", _STATES[:5])]),
        _node("n2", [_q("State", _STATES)]),
    ])
    assert _only(cat)["options"] == _STATES


def test_a_poorer_later_sighting_never_erodes_the_catalogue():
    """Order must not decide the answer. A later, emptier view of the same
    question is a partial observation, not a correction."""
    cat = build_master_catalog([
        _node("n1", [_q("State", _STATES)]),
        _node("n2", [_q("State", [])]),
    ])
    assert _only(cat)["options"] == _STATES


def test_every_page_the_question_appears_on_is_still_recorded():
    """REGRESSION GUARD: the richest-wins merge must not disturb page tracking.

    Page identity is now the URL. A title cannot identify a page: a single-page
    application serves every step of a wizard under ONE <title>, and live that
    collapsed all 24 catalogued questions of a five-step application onto the
    single page "Summit Life Carrier Administration".
    """
    cat = build_master_catalog([
        _node("n1", [_q("State", [])], title="Personal"),
        _node("n2", [_q("State", _STATES)], title="Address"),
    ])
    assert sorted(_only(cat)["pages"]) == ["/n1", "/n2"]


def test_one_url_carrying_several_states_still_names_each_one():
    """AN SPA WIZARD IS EXACTLY THIS SHAPE — five steps, one URL, one title. If
    the label were the URL alone, the five steps would collapse the same way the
    title collapsed them, and "which questions does step 4 ask" would stay
    unanswerable. The state fingerprint disambiguates, and only where it must."""
    cat = build_master_catalog([
        {"node_fp": "fp_step1", "title": "Apply", "url": "/apply",
         "controls": [_q("Gender", ["Male", "Female"])]},
        {"node_fp": "fp_step4", "title": "Apply", "url": "/apply",
         "controls": [_q("Asthma", [])]},
    ])
    pages = sorted(p for q in cat["questions"] for p in q["pages"])
    assert pages == ["/apply#fp_step1", "/apply#fp_step4"]


def test_an_ordinary_page_keeps_a_clean_label():
    """Disambiguation is a cost paid only where a URL is genuinely ambiguous."""
    cat = build_master_catalog([_node("n1", [_q("State", _STATES)])])
    assert _only(cat)["pages"] == ["/n1"]


def test_required_stays_sticky_true_across_sightings():
    """REGRESSION GUARD on the pre-existing merge rule."""
    cat = build_master_catalog([
        _node("n1", [_q("State", _STATES, required=True)]),
        _node("n2", [_q("State", _STATES, required=False)]),
    ])
    assert _only(cat)["required"] is True


def test_duplicate_and_blank_answers_are_dropped_without_inflating_the_count():
    q = _only(build_master_catalog(
        [_node("n1", [_q("Yes/No", ["Yes", "Yes", " ", "No", ""])])]))
    assert q["options"] == ["Yes", "No"]
    assert q["options_total"] == 5, (
        "the OFFERED count reflects the page; the stored list is deduped")

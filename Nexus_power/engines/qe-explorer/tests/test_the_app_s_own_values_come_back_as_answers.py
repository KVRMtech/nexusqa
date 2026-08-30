"""RUNG 3: THE APPLICATION'S OWN VALUES COME BACK AS ANSWERS.

No generator and no model can invent a customer that EXISTS. A Sales Order
needs one, "Alex Morgan" is refused forever, and that is why business flows
stall at their first document — a data-SOURCE problem wearing a
data-generation costume.

The application answers it. MEASURED (Dolibarr, 2026-08-30), straight off its
own list pages:

    {"Third-party name": "Book Keeping Company", "Customer Code": "CU1108-0004"}
    {"Ref.": "PR2001-0034", "Third-party": "Indian SAS", "Country": "India"}

Referentially real: the record exists, so the form is accepted, and nothing was
invented, so the evidence is as strong as the client's own data.

These tests pin the pool's rules and — the part that matters most — that an
entity stays an ENTITY: a harvested customer travels with its own code and its
own city, never another row's.
"""
from __future__ import annotations

from app.harvest import HarvestPool


DOLIBARR_THIRD_PARTIES = [{
    "headers": ["Third-party name", "Customer Code", "Zip Code", "Country"],
    "entities": [
        {"Third-party name": "Book Keeping Company", "Customer Code": "CU1108-0004",
         "Zip Code": "75001", "Country": "France"},
        {"Third-party name": "Indian SAS", "Customer Code": "CU1702-0020",
         "Zip Code": "110001", "Country": "India"},
    ],
}]


def _pool(grids=None):
    p = HarvestPool()
    p.ingest(grids if grids is not None else DOLIBARR_THIRD_PARTIES)
    return p


# ── what it harvests ───────────────────────────────────────────────────────

def test_a_column_s_values_are_available_by_its_own_header():
    p = _pool()
    assert p.candidates("Customer Code") == ["CU1108-0004", "CU1702-0020"]
    assert p.value_for("Third-party name") == "Book Keeping Company"


def test_the_header_match_ignores_case_and_spacing():
    assert _pool().value_for("  customer   code ") == "CU1108-0004"


def test_a_field_named_around_the_column_still_matches():
    """A form asking "Customer Code *" must find the "Customer Code" column."""
    assert _pool().value_for("Customer Code *") == "CU1108-0004"


def test_a_refused_value_is_not_offered_again():
    p = _pool()
    assert p.value_for("Customer Code", refused=["CU1108-0004"]) == "CU1702-0020"


# ── the entity must stay an entity ─────────────────────────────────────────

def test_a_row_is_kept_whole_so_a_customer_keeps_its_own_code():
    """THE POINT. A bag of strings would pair Indian SAS with France."""
    p = _pool()
    rows = [e for e in p.entities if e.get("Third-party name") == "Indian SAS"]
    assert rows and rows[0]["Country"] == "India"
    assert rows[0]["Customer Code"] == "CU1702-0020"


# ── what it must refuse ────────────────────────────────────────────────────

def test_a_dash_or_placeholder_cell_is_not_a_value():
    p = _pool([{"headers": ["Name", "Notes"],
                "entities": [{"Name": "Acme Ltd", "Notes": "—"},
                             {"Name": "Beta Ltd", "Notes": "n/a"}]}])
    assert p.candidates("Notes") == []
    assert p.candidates("Name") == ["Acme Ltd", "Beta Ltd"]


def test_a_single_column_row_is_not_an_entity_but_its_value_still_counts():
    """Two different questions. "Is this a valid Name?" — yes, one cell answers
    it. "What belongs with it?" — nothing, so it is not an entity."""
    p = _pool([{"headers": ["Name"], "entities": [{"Name": "Acme Ltd"}]}])
    assert p.entities == []
    assert p.candidates("Name") == ["Acme Ltd"]


def test_an_unmatched_field_gets_nothing_rather_than_a_guess():
    """STRICT ON PURPOSE. A loose match would type a customer code into a
    postcode — the same collision the client data library refuses by design."""
    assert _pool().value_for("Policy Number") is None
    assert _pool().value_for("x") is None, "a 1-char label must not match anything"


def test_the_pool_is_bounded():
    big = [{"headers": ["A", "B"],
            "entities": [{"A": f"a{i}", "B": f"b{i}"} for i in range(999)]}]
    p = HarvestPool(max_entities=10)
    p.ingest(big)
    assert len(p.entities) == 10


def test_ingesting_twice_does_not_duplicate_a_value():
    p = _pool()
    p.ingest(DOLIBARR_THIRD_PARTIES)
    assert p.candidates("Country") == ["France", "India"]


# ── where the rung sits in the ladder ──────────────────────────────────────

from app.forms import AnswerKey, PROV_HARVESTED, resolve_field  # noqa: E402
from app.identity_pack import derive  # noqa: E402


def _field(name="Customer Code"):
    return {"name": name, "kind": "text", "input_type": "text"}


def test_a_harvested_value_answers_a_field_the_generator_could_not():
    """THE POINT: a referentially real value, from the application itself."""
    d = resolve_field(_field(), "text", "Customer Code", AnswerKey(),
                      derive("t"), harvest=_pool())
    assert d["value"] == "CU1108-0004"
    assert d["entry"]["provenance"] == PROV_HARVESTED


def test_the_client_s_own_answer_key_still_wins():
    """THE CONTROL. Harvest is rung 3; the client is rung 1."""
    key = AnswerKey(exact={"customer code": "CU-CLIENT-1"})
    d = resolve_field(_field(), "text", "Customer Code", key, derive("t"),
                      harvest=_pool())
    assert d["value"] == "CU-CLIENT-1"
    assert d["entry"]["provenance"] != PROV_HARVESTED


def test_a_field_the_pool_has_never_seen_falls_through_unchanged():
    d = resolve_field(_field("Policy Number"), "text", "Policy Number",
                      AnswerKey(), derive("t"), harvest=_pool())
    assert d["entry"]["provenance"] != PROV_HARVESTED


def test_with_no_pool_the_ladder_behaves_exactly_as_before():
    a = resolve_field(_field(), "text", "Customer Code", AnswerKey(), derive("t"))
    b = resolve_field(_field(), "text", "Customer Code", AnswerKey(), derive("t"),
                      harvest=None)
    assert a["value"] == b["value"]
    assert a["entry"]["provenance"] == b["entry"]["provenance"]

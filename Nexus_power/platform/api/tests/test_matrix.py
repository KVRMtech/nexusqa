"""Which member x environment combinations can actually run.

F7. Everything here was already computable and nowhere visible: an operator learned
that a card was missing, orphaned or unproven by dispatching a run and reading the
refusal. That teaches people to treat a BLOCKED run as noise.

The matrix must agree with the gate exactly — a cell that says ready and then gets
refused is worse than no matrix at all.

Pure - no DB, no live stack.
"""
import pytest

from app.services.test_factory import card_state, matrix

RECIPE = {
    "version": 2,
    "steps": [{"action": "fill", "slot": "email", "label": "Email"},
              {"action": "fill", "slot": "password", "label": "Password"}],
    "slots": [{"name": "email", "type": "secret"}, {"name": "password", "type": "secret"}],
}

MEMBERS = [{"persona_id": "p1", "name": "Member A", "traits": [], "behavior_class": ""},
           {"persona_id": "p2", "name": "Member B", "traits": [], "behavior_class": ""}]

UAT = {"environment_id": "uat", "label": "UAT", "posture": "read_write",
       "is_production": False, "base_url": "https://uat.example.com"}
PROD = {"environment_id": "prod", "label": "Production", "posture": "read_write",
        "is_production": True, "write_authorized": False,
        "base_url": "https://www.example.com"}


def _card(pid, eid, slots=("email", "password"), status="verified", version=2):
    return {"persona_id": pid, "environment_id": eid, "present": True,
            "slot_names": list(slots), "verify_status": status,
            "recipe_version": version, "verified_epoch": "",
            "last_verified_at": "2026-08-01T00:00:00+00:00"}


def _cell(m, pid, eid):
    return m["cells"][f"{pid}::{eid}"]


# ── the grid ─────────────────────────────────────────────────────────────────

def test_every_member_times_every_environment_gets_a_cell():
    m = matrix.build(personas=MEMBERS, environments=[UAT, PROD], cards=[], recipe=RECIPE)
    assert len(m["cells"]) == 4
    assert m["summary"]["total"] == 4


def test_a_provisioned_and_proven_member_is_ready():
    m = matrix.build(personas=MEMBERS, environments=[UAT],
                     cards=[_card("p1", "uat")], recipe=RECIPE)
    assert _cell(m, "p1", "uat")["state"] == card_state.READY
    assert _cell(m, "p1", "uat")["runnable"] is True
    assert _cell(m, "p2", "uat")["state"] == card_state.NO_CARD


def test_an_orphaned_card_shows_what_changed():
    m = matrix.build(personas=MEMBERS, environments=[UAT],
                     cards=[_card("p1", "uat", slots=("member_number", "password"))],
                     recipe=RECIPE)
    c = _cell(m, "p1", "uat")
    assert c["state"] == card_state.STALE_SLOTS
    assert c["runnable"] is False
    assert c["missing_slots"] == ["email"]


def test_a_card_for_one_environment_does_not_cover_another():
    """The whole reason the matrix is two-dimensional."""
    m = matrix.build(personas=MEMBERS, environments=[UAT, PROD],
                     cards=[_card("p1", "uat")], recipe=RECIPE)
    assert _cell(m, "p1", "uat")["runnable"] is True
    assert _cell(m, "p1", "prod")["state"] == card_state.NO_CARD


# ── the two axes stay apart ──────────────────────────────────────────────────

def test_a_default_deny_production_blocks_on_POSTURE_not_on_the_card():
    """Every member is blocked equally there. Saying 'no card' would send the
    operator to provision credentials that were never the problem."""
    m = matrix.build(personas=MEMBERS, environments=[PROD],
                     cards=[_card("p1", "prod")], recipe=RECIPE, mutating=True)
    c = _cell(m, "p1", "prod")
    assert c["state"] == "blocked_posture"
    assert c["runnable"] is False
    assert c["reason"] == "environment_policy"
    assert "production" in c["note"].lower()


def test_the_CREDENTIAL_problem_is_named_first_when_a_cell_has_both():
    """Fixing the posture would still leave the run refused, so naming the posture
    first would send the operator round a loop."""
    m = matrix.build(personas=MEMBERS, environments=[PROD],
                     cards=[_card("p1", "prod", slots=("wrong",))],
                     recipe=RECIPE, mutating=True)
    assert _cell(m, "p1", "prod")["state"] == card_state.STALE_SLOTS


def test_a_read_only_run_is_allowed_on_a_locked_environment():
    """gate_dispatch permits a non-mutating run there, so the matrix must say which
    question it answered rather than implying the target is unusable."""
    m = matrix.build(personas=MEMBERS, environments=[PROD],
                     cards=[_card("p1", "prod")], recipe=RECIPE, mutating=False)
    assert _cell(m, "p1", "prod")["runnable"] is True
    assert m["mutating"] is False


def test_an_authorized_production_environment_is_not_blocked():
    env = dict(PROD, write_authorized=True)
    m = matrix.build(personas=MEMBERS, environments=[env],
                     cards=[_card("p1", "prod")], recipe=RECIPE, mutating=True)
    assert _cell(m, "p1", "prod")["runnable"] is True


def test_a_posture_blocked_cell_is_never_reported_as_proven():
    """A proof about the credential says nothing about whether the target permits
    the run; showing 'proven' on a cell that cannot run reads as a green light."""
    m = matrix.build(personas=MEMBERS, environments=[PROD],
                     cards=[_card("p1", "prod")], recipe=RECIPE, mutating=True)
    assert _cell(m, "p1", "prod")["proven"] is False


# ── prerequisites and edges ──────────────────────────────────────────────────

def test_with_no_recorded_login_every_cell_says_so():
    m = matrix.build(personas=MEMBERS, environments=[UAT], cards=[], recipe=None)
    assert all(c["state"] == card_state.NO_RECIPE for c in m["cells"].values())
    assert m["summary"]["has_recipe"] is False


def test_a_legacy_member_is_runnable_without_a_card():
    m = matrix.build(personas=MEMBERS, environments=[UAT], cards=[], recipe=RECIPE,
                     legacy_persona_ids={"p1"})
    assert _cell(m, "p1", "uat")["runnable"] is True
    assert _cell(m, "p2", "uat")["state"] == card_state.NO_CARD
    assert m["members"][0]["legacy"] is True


def test_no_members_or_no_environments_yields_an_empty_grid_not_an_error():
    for personas, envs in (([], [UAT]), (MEMBERS, []), ([], [])):
        m = matrix.build(personas=personas, environments=envs, cards=[], recipe=RECIPE)
        assert m["cells"] == {}
        assert m["summary"]["total"] == 0


def test_the_summary_counts_what_the_screen_should_lead_with():
    m = matrix.build(
        personas=MEMBERS, environments=[UAT],
        cards=[_card("p1", "uat"), _card("p2", "uat", slots=("wrong",))],
        recipe=RECIPE)
    assert m["summary"]["runnable"] == 1
    assert m["summary"]["proven"] == 1
    assert m["summary"]["counts"]["stale_slots"] == 1


def test_worst_first_ordering_is_declared_so_the_screen_can_lead_with_breakage():
    assert matrix.CELL_ORDER[0] == "stale_slots"
    assert matrix.CELL_ORDER[-1] == "ready"


def test_no_secret_can_reach_the_matrix():
    """It is built from slot NAMES only — the card status projection never carries
    ciphertext or values."""
    m = matrix.build(personas=MEMBERS, environments=[UAT],
                     cards=[dict(_card("p1", "uat"), blob=b"x")], recipe=RECIPE)
    assert "blob" not in repr(m)


# ── the matrix and the gate must not disagree ────────────────────────────────

def test_a_cell_marked_runnable_is_exactly_what_card_state_would_allow():
    """If these two ever diverge, the screen becomes a liar: a cell says ready and
    the run is refused, or worse the reverse."""
    cards = [_card("p1", "uat"),
             _card("p2", "uat", slots=("member_number",), status="unverified")]
    m = matrix.build(personas=MEMBERS, environments=[UAT], cards=cards, recipe=RECIPE)
    for c in cards:
        direct = card_state.evaluate(recipe=RECIPE, card=c, environment=UAT)
        cell = _cell(m, c["persona_id"], c["environment_id"])
        assert cell["state"] == direct["state"]
        assert cell["runnable"] == direct["runnable"]

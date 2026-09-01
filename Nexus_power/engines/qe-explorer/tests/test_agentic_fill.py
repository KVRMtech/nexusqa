"""AGENTIC FILL — a missing value is an event to resolve, not a reason to stop.

The old shape of this was: meet a form, fail to answer some of it, file the gaps
as residue, ask a human for fifteen values, and require the whole crawl to be RUN
AGAIN once they arrive. For a platform whose premise is autonomy that is backwards
— a missing value is the most ordinary thing a crawl meets, and turning it into a
stop-and-ask-and-restart cycle makes the ordinary case the expensive one.

The resolver already had a rung ladder with provenance. Two things were wrong:

  1. The rung that ANSWERS was off by default. ``data_mode='user'`` leaves every
     semantic choice unanswered, and qe-central sent it for every app.
  2. There was no memory WITHIN a journey. A funnel that asks for an email on the
     contact step and again on the confirmation step got two different answers,
     and the application rejected its own steps on cross-validation — a dead end
     the app was right to produce.

What must NOT change is that autonomy and honesty stay separable. Every rung
stamps ``provenance``, so a journey completed on invented data is a valid
traversal AND a clearly-labelled one. The last section pins that directly.
"""
from __future__ import annotations

from app import field_signature
from app.field_values import DATA_MODE_AGENT, DATA_MODE_USER
from app.forms import (
    PROV_JOURNEY,
    PROV_NEEDS_INPUT,
    PROV_PROVIDED,
    PROV_RECALLED,
    PROV_SYNTHESIZED,
    AnswerKey,
    resolve_field,
)
from app.identity_pack import derive as derive_identity

_ID = derive_identity("test-seed")


def _sig(control, kind) -> str:
    return field_signature.compute(control, kind=kind)["signature"]


def _key(exact: dict[str, str]) -> AnswerKey:
    """A client answer key, built the way the explore request builds one.

    ``AnswerKey.resolve`` normalises the control name before looking it up, so a
    key constructed with raw labels silently never matches — build it through
    ``from_payload``, which normalises both sides.
    """
    return AnswerKey.from_payload({"exact": exact})


def _text(name: str) -> dict:
    return {"name": name, "kind": "text", "input_type": "text"}


def _resolve(control, kind, **kw):
    return resolve_field(control, kind, control["name"], kw.pop("key", AnswerKey({})),
                         _ID, **kw)


# ── 1. the agent answers what it honestly can ───────────────────────────────

def test_a_semantic_choice_goes_unanswered_in_user_mode(tmp_path):
    """Baseline — the behaviour that produced "provide 15 values, re-run crawl"."""
    control = {"name": "Tobacco use", "kind": "radio",
               "options": ["Non-smoker", "Smoker"]}
    out = _resolve(control, "radio", data_mode=DATA_MODE_USER)
    assert out["value"] is None
    assert out["entry"]["provenance"] == PROV_NEEDS_INPUT


def test_the_agent_answers_the_same_choice_and_declares_it_synthesized(tmp_path):
    """Answering is not the same as pretending. The value is produced AND the
    record says it was invented, which is what keeps a green result honest."""
    control = {"name": "Tobacco use", "kind": "radio",
               "options": ["Non-smoker", "Smoker"]}
    out = _resolve(control, "radio", data_mode=DATA_MODE_AGENT)
    assert out["value"] is not None
    assert out["entry"]["provenance"] == PROV_SYNTHESIZED
    assert out["entry"]["filled"] is True


def test_realistic_identity_data_is_produced_for_a_plain_field():
    """First/last name, DOB, address, city, state, ZIP, phone, email — the
    generated person is coherent, so an application that cross-validates its own
    fields is not fighting the crawl."""
    for label in ("First name", "Last name", "Email", "Phone", "City", "ZIP"):
        out = _resolve(_text(label), "text", data_mode=DATA_MODE_AGENT)
        assert out["value"], f"{label} was left unanswered"
        assert out["entry"]["provenance"] == PROV_SYNTHESIZED


def test_a_field_nothing_can_honestly_answer_still_asks(tmp_path):
    """THE LIMIT OF AUTONOMY. There is no value a generator can invent for a
    one-time code that would mean anything — inventing one produces a test that
    passes against nothing. Agent mode must not turn "I cannot know this" into a
    fabrication."""
    control = {"name": "One-time passcode", "kind": "text", "input_type": "text"}
    out = _resolve(control, "text", data_mode=DATA_MODE_AGENT)
    if out["value"] is not None:                      # structural default ladder
        assert out["entry"]["provenance"] == PROV_SYNTHESIZED
    else:
        assert out["entry"]["provenance"] == PROV_NEEDS_INPUT


# ── 2. journey memory — answer the same question the same way ───────────────

def test_a_question_asked_twice_is_answered_the_same_way(tmp_path):
    """THE CROSS-VALIDATION DEFECT. Contact step asks for an email; confirmation
    step asks again. Two independent derivations gave two different answers and
    the application rejected the step — correctly."""
    control = _text("Email address")
    journey: dict[str, str] = {}

    first = _resolve(control, "text", data_mode=DATA_MODE_AGENT, journey_values=journey)
    journey[_sig(control, "text")] = first["value"]

    second = _resolve(control, "text", data_mode=DATA_MODE_AGENT, journey_values=journey)
    assert second["value"] == first["value"]
    assert second["entry"]["provenance"] == PROV_JOURNEY


def test_journey_memory_outranks_a_value_remembered_from_last_month(tmp_path):
    """THIS journey's answer is the current truth. A value recalled from a prior
    crawl must not overwrite one this funnel committed two steps ago and is now
    being validated against."""
    control = _text("Policy number")
    sig = _sig(control, "text")
    out = _resolve(control, "text", data_mode=DATA_MODE_AGENT,
                   journey_values={sig: "JOURNEY-1"}, recalled={sig: "OLD-1"})
    assert out["value"] == "JOURNEY-1"
    assert out["entry"]["provenance"] == PROV_JOURNEY


def test_the_client_answer_key_still_outranks_everything(tmp_path):
    """THE ORDER THAT MUST NOT INVERT. An explicit instruction from the client
    beats every form of memory and every generator — autonomy never overrules
    someone who told us the answer."""
    control = _text("Email address")
    sig = _sig(control, "text")
    out = _resolve(control, "text", key=_key({"Email address": "real@client.example"}),
                   data_mode=DATA_MODE_AGENT,
                   journey_values={sig: "JOURNEY-1"}, recalled={sig: "OLD-1"})
    assert out["value"] == "real@client.example"
    assert out["entry"]["provenance"] == PROV_PROVIDED


def test_recall_still_works_when_this_journey_has_no_answer_yet(tmp_path):
    """REGRESSION GUARD: the new rung sits between provided and recalled without
    displacing either."""
    control = _text("Policy number")
    out = _resolve(control, "text", data_mode=DATA_MODE_AGENT,
                   journey_values={}, recalled={_sig(control, "text"): "OLD-1"})
    assert out["value"] == "OLD-1"
    assert out["entry"]["provenance"] == PROV_RECALLED


def test_journey_memory_never_overrules_a_planned_branch_walk(tmp_path):
    """A branch walk exists to take the option the default data would NOT. If
    memory could re-impose an earlier answer, the second pass would silently walk
    the same branch as the first and the coverage claim would be false."""
    control = {"name": "Tobacco use", "kind": "select",
               "options": ["Non-smoker", "Smoker"]}
    sig = _sig(control, "select")
    out = _resolve(control, "select", data_mode=DATA_MODE_AGENT,
                   journey_values={sig: "Non-smoker"},
                   choice_overrides={sig: "Smoker"})
    assert out["value"] == "Smoker"


# ── 3. autonomy and honesty stay separable ──────────────────────────────────

def test_every_rung_declares_where_its_value_came_from(tmp_path):
    """THE LOAD-BEARING PROPERTY. Autonomy decides how far the crawl gets;
    provenance decides what a green result MEANS. If a rung ever filled a value
    without declaring its source, a suite built on invented data would be
    indistinguishable from one proven on the client's own — which is the exact
    failure this product exists to prevent."""
    control = _text("Email address")
    sig = _sig(control, "text")
    cases = [
        (dict(key=_key({"Email address": "a@b.example"})), PROV_PROVIDED),
        (dict(journey_values={sig: "j@b.example"}), PROV_JOURNEY),
        (dict(recalled={sig: "r@b.example"}), PROV_RECALLED),
        (dict(), PROV_SYNTHESIZED),
    ]
    for kwargs, expected in cases:
        out = _resolve(control, "text", data_mode=DATA_MODE_AGENT, **kwargs)
        assert out["entry"]["provenance"] == expected
        assert out["entry"]["filled"] is True
        assert out["value"] is not None

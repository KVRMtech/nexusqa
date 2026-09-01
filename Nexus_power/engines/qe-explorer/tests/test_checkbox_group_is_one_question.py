"""A MULTI-SELECT IS ONE QUESTION, NOT N YES/NO QUESTIONS.

Live, on a five-step insurance application: the Health step asks "Health
Conditions — select at least one" over eight checkboxes. The catalogue recorded
EIGHT independent questions, each with the answer set {checked, unchecked}, and
every one of them ``needs_input``. That is a description of a form the
application does not have, and any test generated from it would exercise a
fiction — while the residue asked a human to supply eight answers to a question
that has one.

The cause was structural: ``groupKeyOf`` returned "" for anything that was not a
radio, so a checkbox could not carry a group at all, and GROUP_ASSEMBLE never
saw one.

Grouping is on DECLARED signals only — a shared ``name`` (the ``name="x[]"``
array every server-side framework renders) or an explicit ``fieldset`` /
``role=group``. Proximity is deliberately NOT a signal: a "Remember me" next to
a "Subscribe to newsletter" is two questions, and merging them would answer one
and silently drop the other from the residue.

The consequence of grouping is that the fill must change with it. The browser
enforces exclusivity for radios and does NOT for checkboxes, so the same
"fill each member in turn" loop that yields one answer for a radio group yields
EVERY BOX CHECKED for a checkbox group — on this form, an applicant disclosing
every condition on the list because of the order the fill iterated in.
"""
from __future__ import annotations

import hashlib

import pytest

from app import field_values
from app.field_values import DATA_MODE_AGENT, DATA_MODE_USER
from app.forms import (AnswerKey, PROV_GROUP_SIBLING, _synthesize_default,
                       _wants_checked, _wants_checked_control, resolve_field)
from app.identity_pack import derive
from app.inventory import build_inventory
from app.inventory_js import INVENTORY_JS


CONDITIONS = ["None", "Controlled Hypertension", "Type 2 Diabetes",
              "Elevated BMI", "Sleep Apnea (Treated)", "Asthma"]


def _raw(name: str, *, kind: str = "checkbox", group_key: str = "name:f:conds"):
    return {"role": kind, "name": name, "name_source": "content",
            "best_effort": False, "kind": kind, "tag": "input",
            "input_type": kind, "options": [], "required": False,
            "disabled": False, "frame_selector": "", "testid": "",
            "css_hint": "", "value_committed": "false", "group_key": group_key,
            "landmark": {"role": "", "name": ""}}


def _built(names=CONDITIONS, **kw):
    return build_inventory([_raw(n, **kw) for n in names], None,
                           url="https://app/apply")


# ─── the capture: a checkbox can carry a group at all ────────────────────────

def test_the_extractor_groups_checkboxes_not_only_radios():
    assert 'var isCheck = (tag === "input" && it === "checkbox")' in INVENTORY_JS
    assert 'if (!isRadio && !isCheck) return ""' in INVENTORY_JS


def test_only_a_declared_container_groups_a_checkbox():
    """``role=group`` and ``fieldset`` are declarations. A shared parent div is
    not — grouping on it would merge unrelated consents into one question."""
    assert '(isCheck && r === "group")' in INVENTORY_JS
    assert 'lc(cur.tagName) === "fieldset"' in INVENTORY_JS


# ─── assembly ────────────────────────────────────────────────────────────────

def test_a_declared_checkbox_group_becomes_one_question():
    built = _built()
    ids = {c["group_id"] for c in built}
    assert len(ids) == 1 and ids != {""}
    for c in built:
        assert c["group_options"] == CONDITIONS
        assert c["group_size"] == len(CONDITIONS)


def test_an_ungrouped_checkbox_is_untouched():
    """A lone consent is a boolean, not a question with members."""
    built = build_inventory([_raw("I agree to the terms", group_key="")], None,
                            url="https://app/x")
    assert not built[0].get("group_id")


def test_a_lone_member_is_not_a_question():
    built = _built(names=["I agree"])
    assert not built[0].get("group_id")


def test_radios_and_checkboxes_in_one_fieldset_stay_separate_questions():
    """A fieldset yields ONE container key for everything inside it. Merging the
    two kinds would enumerate answers belonging to different questions."""
    raws = ([_raw(n, group_key="grp:fs1") for n in ("Email", "SMS")]
            + [_raw(n, kind="radio", group_key="grp:fs1") for n in ("Yes", "No")])
    built = build_inventory(raws, None, url="https://app/x")
    checks = {c["group_id"] for c in built if c["kind"] == "checkbox"}
    radios = {c["group_id"] for c in built if c["kind"] == "radio"}
    assert len(checks) == 1 and len(radios) == 1
    assert checks.isdisjoint(radios)


def test_radio_group_ids_keep_their_historical_hash():
    """They key remembered branch-walk overrides across crawls. Re-hashing them
    would silently orphan every plan a previous crawl recorded — the walk would
    look fine and quietly stop honouring its own plan."""
    built = build_inventory(
        [_raw(n, kind="radio", group_key="name:f:tobacco") for n in ("No", "Yes")],
        None, url="https://app/x")
    expected = hashlib.sha256(b"\x1fname:f:tobacco").hexdigest()[:32]
    assert built[0]["group_id"] == expected


# ─── the fill: one question, one answer ──────────────────────────────────────

def _resolve_all(built, data_mode=DATA_MODE_AGENT):
    ident = derive("seed")
    return [resolve_field(c, c["kind"], c["name"], AnswerKey.from_payload({}),
                          ident, data_mode=data_mode) for c in built]


def test_exactly_one_member_is_answered():
    out = _resolve_all(_built())
    filled = [o for o in out if o["value"] is not None]
    assert len(filled) == 1, (
        "every box was checked — the applicant disclosed every condition on the "
        "list because of the order the fill iterated in")


def test_the_answer_is_the_one_that_asserts_the_least():
    out = _resolve_all(_built())
    answered = [o for o in out if o["value"] is not None][0]
    assert answered["value"] == "None"


def test_dom_order_does_not_decide_the_answer():
    """An app that lists "None" last must not have a condition disclosed on its
    behalf just because a positive option happened to come first."""
    reordered = [n for n in CONDITIONS if n != "None"] + ["None"]
    out = _resolve_all(_built(names=reordered))
    answered = [o for o in out if o["value"] is not None][0]
    assert answered["value"] == "None"


def test_a_group_with_no_negative_option_still_gets_answered():
    """Refusing to answer would put the walk back where it started."""
    names = ["Auto", "Home", "Umbrella"]
    out = _resolve_all(_built(names=names))
    filled = [o for o in out if o["value"] is not None]
    assert len(filled) == 1 and filled[0]["value"] in names


def test_the_unanswered_members_never_reach_the_human_ask():
    """The question WAS answered. Listing its other members as residue asks
    someone to supply a value that has already been chosen."""
    out = _resolve_all(_built())
    siblings = [o for o in out if o["value"] is None]
    assert len(siblings) == len(CONDITIONS) - 1
    assert all(o["entry"]["provenance"] == PROV_GROUP_SIBLING for o in siblings)


def test_a_negative_member_is_selected_and_not_switched_off():
    """The literal-word rule reads "No"/"None" as intent to UNCHECK. Applied to
    the member that IS the answer, the question ends unanswered while the ledger
    records an answer — and the negative member is the one we prefer, so the
    group's answer is exactly the one it would fail to select."""
    grouped = {"group_id": "g1", "kind": "checkbox"}
    assert _wants_checked_control(grouped, "checkbox", "None") is True
    assert _wants_checked_control(grouped, "checkbox", "No") is True
    # An ungrouped toggle keeps two-way intent, exactly as before.
    assert _wants_checked_control({}, "checkbox", "no") is False
    assert _wants_checked({}, "checkbox") is not None  # sanity: still callable


def test_a_required_member_is_not_auto_checked_on_its_own_account():
    """``required`` on one member of a multi-select is a statement about that
    box. Honouring it per-member checks every required box in the group."""
    member = {"kind": "checkbox", "required": True, "group_id": "g1",
              "input_type": "checkbox", "options": []}
    assert _synthesize_default(member, "checkbox", "Type 2 Diabetes") is None
    # A LONE required consent is still cleared — that gate is not a question.
    lone = {"kind": "checkbox", "required": True, "input_type": "checkbox",
            "options": []}
    assert _synthesize_default(lone, "checkbox", "I agree") == "true"


def test_user_mode_leaves_the_question_to_the_client():
    """A multi-select decides which business path is exercised, for the same
    reason a radio group does. The crawl must not choose and then not say so."""
    out = _resolve_all(_built(), data_mode=DATA_MODE_USER)
    assert all(o["value"] is None for o in out)


def test_an_ungrouped_consent_is_unaffected_in_user_mode():
    ident = derive("seed")
    lone = build_inventory([dict(_raw("I agree", group_key=""), required=True)],
                           None, url="https://app/x")[0]
    got = resolve_field(lone, "checkbox", "I agree", AnswerKey.from_payload({}),
                        ident, data_mode=DATA_MODE_USER)
    assert got["value"] == "true"


def test_the_group_enumeration_is_the_catalogued_answer_set():
    """What the catalogue should hold: one question, six answers — not six
    questions each answered {checked, unchecked}."""
    built = _built()
    assert field_values._options(built[0]) == CONDITIONS


def test_a_group_member_is_never_mistaken_for_a_placeholder():
    """The placeholder filter answers a question about a DROPDOWN — is this
    first entry a prompt ("Select…", "None") or an answer? A set of real
    controls has no prompt among them, and running the filter over them deleted
    the member labelled "None": the group's only negative answer, and the one
    the fill prefers precisely because it asserts nothing. The question was then
    answered with the first POSITIVE option, disclosing a condition on the
    applicant's behalf.

    Latent for radios on the same line — a Yes/No/None radio group lost its
    "None" the same way — so this pins both."""
    for kind in ("checkbox", "radio"):
        built = build_inventory(
            [_raw(n, kind=kind, group_key=f"name:f:{kind}")
             for n in ("None", "Yes", "No")], None, url="https://app/x")
        assert field_values._options(built[0]) == ["None", "Yes", "No"], kind


def test_a_dropdown_still_has_its_placeholder_dropped():
    """The filter is right where it was designed to be used — this must not
    become a licence to answer a select with its own prompt."""
    select = {"kind": "select", "options": ["Select…", "Male", "Female"]}
    assert field_values._options(select) == ["Male", "Female"]

"""Recipe-builder side of verify-documents + Home-reached oracle (Phase 1/3).

Pins the pure builders that PRODUCE the recipe shape the compiler interpreter
replays (the interpreter itself is pinned in test_verify_documents_oracle.py):

    Member (form-login)  ->  [Verify-documents, OPTIONAL]  ->  [assert_home ORACLE]

All pure functions — no DB, no browser.
"""
from app.services.test_factory import persona_store as store

# Actions the compiler recipe interpreter handles (compiler.py _AUTH_SETUP_TS).
# The builder must never emit an action outside this set.
INTERPRETER_ACTIONS = {"goto", "fill", "click", "wait", "assert_home"}

CFG = {
    "login_path": "/login",
    "fields": [
        {"label": "Member number", "value": "member_number"},
        {"label": "Password", "value": "password"},
    ],
    "submit_label": "Continue",
}


# ── assert_home step (the Home-reached oracle) ───────────────────────────────
def test_assert_home_step_from_url_pattern():
    assert store._assert_home_step({"url_pattern": "/dashboard"}) == {
        "action": "assert_home", "url_pattern": "/dashboard"}


def test_assert_home_step_from_selector_text_and_timeout():
    s = store._assert_home_step(
        {"selector": "#home", "expect_text": "Welcome", "timeout": "9000"})
    assert s["action"] == "assert_home"
    assert s["selector"] == "#home"
    assert s["expect_text"] == "Welcome"
    assert s["timeout"] == 9000  # coerced to int


def test_assert_home_refuses_a_signal_less_oracle():
    # An oracle that asserts nothing would always pass — that is green-washing.
    assert store._assert_home_step(None) is None
    assert store._assert_home_step({}) is None
    assert store._assert_home_step({"timeout": 5000}) is None  # timeout is not a signal


# ── verify-documents optional marking ────────────────────────────────────────
def test_verify_document_steps_marks_actions_optional_without_mutating_input():
    recorded = [
        {"action": "click", "name": "Agree"},
        {"action": "fill", "slot": "initials", "label": "Initials"},
    ]
    out = store._verify_document_steps(recorded)
    assert all(s.get("optional") is True for s in out)
    assert "optional" not in recorded[0]  # input not mutated


def test_verify_document_steps_leaves_goto_wait_unconditional():
    out = store._verify_document_steps(
        [{"action": "goto", "path": "/docs"}, {"action": "wait", "state": "networkidle"}])
    assert all("optional" not in s for s in out)


# ── full recipe assembly ─────────────────────────────────────────────────────
def test_build_login_recipe_is_backward_compatible():
    # No verify_documents + no home == the plain form-login recipe, unchanged.
    assert store.build_login_recipe(CFG) == store._recipe_from_form_login(CFG)


def test_build_login_recipe_full_shape_and_order():
    steps, slots = store.build_login_recipe(
        CFG,
        verify_documents=[{"action": "click", "name": "Sign document"}],
        home={"selector": "#dashboard"},
    )
    actions = [s["action"] for s in steps]
    # Member (goto, fill*, click, wait) -> verify-doc (optional click) -> assert_home
    assert actions[0] == "goto"
    assert actions[-1] == "assert_home"
    assert actions.count("assert_home") == 1
    assert actions.index("assert_home") > actions.index("wait")
    vdoc = [s for s in steps
            if s.get("action") == "click" and s.get("name") == "Sign document"]
    assert vdoc and vdoc[0].get("optional") is True
    # extra stages don't disturb the credential slots
    assert {s["name"] for s in slots} == {"member_number", "password"}


def test_builder_never_emits_an_action_the_interpreter_cannot_replay():
    steps, _ = store.build_login_recipe(
        CFG,
        verify_documents=[
            {"action": "click", "name": "Agree"},
            {"action": "fill", "slot": "pin", "label": "PIN"},
        ],
        home={"url_pattern": "/home"},
    )
    assert set(s["action"] for s in steps) <= INTERPRETER_ACTIONS

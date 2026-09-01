"""Verify-documents branching + Home-reached oracle in the login recipe interpreter.

Pins the ADDITIVE capability added for the record-once / run-anywhere plan
(Phase 1/3 — the login-sequence stages Environment -> Member -> Verify-documents
-> Home -> Scenario):

- an OPTIONAL step (a verify-documents interstitial some members hit and others
  skip) is attempted with a short timeout and SKIPPED when absent, instead of
  aborting the whole login as "recipe drift";
- an ``assert_home`` step gates login success on the logged-in HOME state actually
  being reached -- not merely on the recorded steps completing -- so a member who
  went through verify-documents and one who skipped it both converge at Home;
- an UNRECORDED interstitial that blocks Home is SURFACED (login fails honestly,
  no session written), never papered over with a fabricated session.

All assertions are on the static interpreter (``compiler._AUTH_SETUP_TS``) — the
recipe data itself flows through a runtime JSON file, so the interpreter code is
constant and directly assertable with no DB or browser.
"""
from app.services.script_factory import compiler

SETUP = compiler._AUTH_SETUP_TS


def test_interpreter_handles_the_verify_documents_recipe_shape():
    # Every action a verify-documents login recipe emits must be one the recipe
    # interpreter actually handles: goto -> fill* -> click -> (optional verify-doc)
    # -> assert_home. Mirrors test_persona_env_r2's builder<->interpreter contract,
    # extended for the two new capabilities.
    for action in ("goto", "fill", "click", "wait", "assert_home"):
        assert ("action === '" + action + "'") in SETUP, (
            "recipe interpreter does not handle action %r" % action
        )


def test_optional_step_is_skipped_not_aborted():
    # An optional step is attempted with a short timeout and skipped when absent.
    assert "step.optional" in SETUP
    assert "optTimeout" in SETUP
    assert "not applicable -- skipping" in SETUP
    # Optional failures `continue`; they do NOT fall through to the drift-abort.
    assert "if (step.optional) {" in SETUP


def test_home_reached_oracle_present():
    # Success is a reached STATE, not a completed step count.
    assert "HOME-REACHED ORACLE" in SETUP
    assert "action === 'assert_home'" in SETUP
    # The oracle waits on url / selector / text — a real landing state.
    assert "waitForURL" in SETUP
    assert "state: 'visible'" in SETUP
    assert "homeAsserted = true" in SETUP


def test_unreached_home_is_surfaced_not_fabricated():
    # An assert_home failure surfaces honestly and writes NO authenticated session
    # (never green-wash an unverified landing).
    assert "did NOT reach Home" in SETUP
    assert "not fabricating a session" in SETUP


def test_backward_compatible_with_existing_recipes():
    # Existing behaviour is preserved: required-step drift still aborts, and the
    # steps-completed success path still fires (now annotated when Home was asserted).
    assert "recipe drift at step" in SETUP
    assert "recipe login OK" in SETUP
    assert "home reached" in SETUP  # appended only when an assert_home step ran

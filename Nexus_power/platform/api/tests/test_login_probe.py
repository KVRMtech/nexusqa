"""'verified' must mean a login actually happened.

F3. The consumer collapsed every login outcome into `"recipe login OK" in output`,
and that one line is why a credential card could be stamped **verified** when
nothing proved anyone got in:

  * `recipe login OK (7 steps)` means the steps REPLAYED. assert_home is optional,
    so a recipe recorded without a logged-in checkpoint emits no oracle at all —
    steps run, cookie jar is anonymous, line still prints.
  * `form login OK` is printed unconditionally after a click + networkidle, with no
    assertion of any kind. A rejected password prints it.

Most dangerous on a permissive test app where every credential "works" — precisely
where a false `verified` is invisible.

The strings pinned here are the ones the compiled globalSetup actually emits
(compiler.py:2137-2225). If that template changes, these tests fail — which is the
point: the parser and the emitter must not drift.

Pure — no DB, no runner.
"""
import pytest

from app.services.test_factory import login_probe as lp


# Verbatim from compiler.py's _AUTH_SETUP_TS.
OK_HOME = "[nexus-auth] recipe login OK (7 steps, home reached) -- wrote ./vkpower.auth.json"
OK_STEPS = "[nexus-auth] recipe login OK (7 steps) -- wrote ./vkpower.auth.json"
OK_FORM = "[nexus-auth] form login OK -- wrote ./vkpower.auth.json"
NOT_HOME = ("[nexus-auth] login did NOT reach Home at step 5 -- unrecorded interstitial? "
            "surfacing, not fabricating a session: Timeout 15000ms exceeded.")
DRIFT = "[nexus-auth] recipe drift at step 3 (click): locator.click: Timeout 30000ms exceeded."
MISSING = "[nexus-auth] recipe login: credential env not set for slots pin,password -- skipping."
FORM_MISSING = "[nexus-auth] form login configured but credentials env not set -- skipping."
SKIPPED = "[nexus-auth] login skipped: net::ERR_CONNECTION_REFUSED"


# ── the one outcome that is proof ────────────────────────────────────────────

def test_only_home_reached_is_proof():
    v = lp.read_outcome(OK_HOME)
    assert v["outcome"] == lp.PROVEN
    assert v["proven"] is True
    assert v["step"] == 7


def test_steps_replayed_without_a_checkpoint_is_NOT_proof():
    """THE F3 DEFECT. This line satisfied `"recipe login OK" in output` and stamped
    the card verified. The steps ran; nobody was shown to be logged in."""
    v = lp.read_outcome(OK_STEPS)
    assert v["outcome"] == lp.STEPS_ONLY
    assert v["proven"] is False


def test_form_login_OK_is_NEVER_proof():
    """It is printed unconditionally after a click. A rejected password prints it."""
    v = lp.read_outcome(OK_FORM)
    assert v["proven"] is False
    assert v["outcome"] == lp.STEPS_ONLY


def test_the_old_substring_test_would_have_accepted_all_three():
    """Pins WHY this module exists: the previous rule cannot tell them apart."""
    for line in (OK_HOME, OK_STEPS, OK_FORM):
        assert ("recipe login OK" in line) or ("form login OK" in line)
    assert [lp.read_outcome(l)["proven"] for l in (OK_HOME, OK_STEPS, OK_FORM)] \
        == [True, False, False]


# ── failures, each attributed to the right thing ─────────────────────────────

def test_a_missing_slot_is_a_CREDENTIAL_problem_and_names_the_slots():
    v = lp.read_outcome(MISSING)
    assert v["outcome"] == lp.MISSING_CREDENTIALS
    assert v["slots"] == ["pin", "password"]
    assert v["attribution"] == "credential"
    assert v["proven"] is False


def test_home_not_reached_is_a_RECIPE_problem_not_a_credential_one():
    """Blaming the card here sends the operator to re-type a password that was
    never wrong — the login worked, an unrecorded interstitial blocked the landing."""
    v = lp.read_outcome(NOT_HOME)
    assert v["outcome"] == lp.NOT_HOME
    assert v["attribution"] == "recipe"
    assert v["step"] == 5
    assert "interstitial" in v["note"]


def test_drift_names_the_step_and_the_action():
    v = lp.read_outcome(DRIFT)
    assert v["outcome"] == lp.DRIFT
    assert v["step"] == 3
    assert v["action"] == "click"
    assert "Timeout" in v["detail"]


def test_a_form_login_with_no_stored_credentials_is_a_credential_problem():
    v = lp.read_outcome(FORM_MISSING)
    assert v["outcome"] == lp.MISSING_CREDENTIALS
    assert v["attribution"] == "credential"


def test_a_setup_that_never_ran_is_not_silently_a_pass():
    v = lp.read_outcome(SKIPPED)
    assert v["outcome"] == lp.NO_ATTEMPT
    assert v["proven"] is False
    assert "ERR_CONNECTION_REFUSED" in v["detail"]


def test_NOTHING_is_ever_attributed_to_the_application_under_test():
    """Every outcome here is a fact about our recording or our configuration. The
    doctrine is that the app is never blamed for our own setup."""
    for line in (OK_HOME, OK_STEPS, OK_FORM, NOT_HOME, DRIFT, MISSING,
                 FORM_MISSING, SKIPPED, "", "garbage"):
        assert lp.read_outcome(line)["attribution"] in ("", "recipe", "credential", "configuration")


# ── the log is a TAIL WINDOW, and retries leave earlier lines in it ──────────

def test_the_LAST_word_wins_when_a_retry_left_an_earlier_line():
    """The runner returns output.slice(-8000), so a first attempt's success line can
    still be present after a later failure. Reading the first match would report a
    login that was subsequently refuted."""
    log = "\n".join([OK_HOME, "...retrying...", DRIFT])
    assert lp.read_outcome(log)["outcome"] == lp.DRIFT

    log = "\n".join([DRIFT, "...retrying...", OK_HOME])
    v = lp.read_outcome(log)
    assert v["outcome"] == lp.PROVEN and v["proven"] is True


def test_a_failure_after_a_success_withdraws_the_proof():
    for bad in (MISSING, NOT_HOME, DRIFT, FORM_MISSING):
        assert lp.read_outcome(OK_HOME + "\n" + bad)["proven"] is False


def test_real_surrounding_playwright_noise_does_not_confuse_it():
    log = ("Running 1 test using 1 worker\n"
           "  [chromium] > vkpower.spec.ts:3:1 > carrier\n"
           + OK_HOME + "\n"
           "  1 passed (4.2s)\n")
    assert lp.read_outcome(log)["proven"] is True


def test_an_unrelated_line_mentioning_login_ok_is_not_matched():
    """Anchored on the emitter's own prefix, so app output cannot forge a pass."""
    assert lp.read_outcome("the page said: recipe login OK")["outcome"] == lp.UNREADABLE
    assert lp.read_outcome("form login OK")["outcome"] == lp.UNREADABLE


# ── nothing recognisable is UNPROVEN, never a pass ───────────────────────────

def test_empty_or_garbage_is_unreadable_and_never_proven():
    for junk in ("", None, "   ", "Error: connection reset", "\x00\x01"):
        v = lp.read_outcome(junk)
        assert v["proven"] is False
        assert v["outcome"] in (lp.UNREADABLE, lp.NO_ATTEMPT)


def test_an_optional_step_skip_is_not_an_outcome_on_its_own():
    """Optional interstitials are skipped by design; that line must not be read as
    a failure, and on its own it says nothing about the login."""
    line = "[nexus-auth] optional step 4 (click) not applicable -- skipping"
    assert lp.read_outcome(line)["outcome"] == lp.UNREADABLE
    assert lp.read_outcome(line + "\n" + OK_HOME)["proven"] is True


# ── the parser and the emitter must not drift apart ─────────────────────────

def test_the_compiler_still_emits_every_line_this_parser_depends_on():
    """The emitter lives in a template string inside the compiler; the reader lives
    here. Nothing links them but these substrings, so if the template is reworded
    this parser silently stops recognising outcomes and everything becomes
    'unreadable'. Fail loudly instead.

    Each fragment below is what the corresponding regex anchors on."""
    from app.services.script_factory import compiler
    ts = compiler._AUTH_SETUP_TS
    for fragment in (
        "[nexus-auth] recipe login OK (",
        "' steps' + (homeAsserted ? ', home reached' : '')",
        "[nexus-auth] form login OK",
        "[nexus-auth] login did NOT reach Home at step ",
        "[nexus-auth] recipe drift at step ",
        "[nexus-auth] recipe login: credential env not set for slots ",
        "[nexus-auth] form login configured but credentials env not set",
        "[nexus-auth] login skipped:",
    ):
        assert fragment in ts, f"the compiler no longer emits: {fragment!r}"


def test_the_home_oracle_is_still_what_sets_homeAsserted():
    """`proven` means the assert_home branch ran. If the compiler stopped setting
    homeAsserted there, 'home reached' would become meaningless."""
    from app.services.script_factory import compiler
    ts = compiler._AUTH_SETUP_TS
    assert "action === 'assert_home'" in ts
    assert "homeAsserted = true;" in ts
    # and the failure path must still refuse to write a session
    assert "not fabricating a session" in ts


def test_exactly_one_outcome_constant_is_proving():
    proving = [c for c in (lp.PROVEN, lp.STEPS_ONLY, lp.NOT_HOME, lp.DRIFT,
                           lp.MISSING_CREDENTIALS, lp.NO_ATTEMPT, lp.UNREADABLE)
               if lp.read_outcome("")["proven"] or c == lp.PROVEN]
    assert proving == [lp.PROVEN]

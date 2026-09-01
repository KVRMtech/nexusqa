"""Read what the login attempt actually reported, instead of matching one substring.

The compiled ``globalSetup`` distinguishes five outcomes and says so in its log.
The consumer collapsed all of them into ``"recipe login OK" in output``, and that
one line is why a credential card can be marked **verified** when no login
happened:

  * ``recipe login OK (7 steps)`` means the recorded steps REPLAYED. It does not
    mean anyone got in. ``assert_home`` is optional — a recipe recorded without a
    logged-in checkpoint emits no oracle at all — so the steps can run, the cookie
    jar can be anonymous, and the line still prints.
  * ``recipe login OK (7 steps, home reached)`` means the logged-in landing was
    ACTUALLY asserted. Only this is proof.
  * ``form login OK`` is printed unconditionally after clicking submit and waiting
    for networkidle. There is no assertion of any kind. A wrong password that
    bounces straight back to the login page prints it. **It is never proof.**

That last one matters most on a permissive test app, where every credential
"works" — exactly the environment where a false ``verified`` is invisible.

The other two outcomes are not failures of the application and must never be
reported as such:

  * ``credential env not set for slots …`` — the card did not cover the login, so
    the whole login was skipped and the suite ran logged out. This is the F4 case;
    the dispatch gate now refuses it before a runner is ever started.
  * ``login did NOT reach Home at step N`` — the steps replayed but the landing was
    not reached, most often an unrecorded interstitial. Blaming the credential card
    for this sends the operator to re-type a password that was never wrong.

Pure — no I/O. Takes the runner's captured stdout/stderr, returns a verdict.
"""
from __future__ import annotations

import re

__all__ = ["read_outcome", "PROVEN", "STEPS_ONLY", "NOT_HOME", "DRIFT",
           "MISSING_CREDENTIALS", "NO_ATTEMPT", "UNREADABLE"]

#: The login was performed AND the recorded logged-in landing was asserted.
PROVEN = "proven"
#: The recorded steps replayed, but nothing confirmed anyone was logged in.
STEPS_ONLY = "steps_completed"
#: Steps replayed; the logged-in landing was never reached.
NOT_HOME = "home_not_reached"
#: A recorded step no longer replays — the login page changed.
DRIFT = "recipe_drift"
#: The card did not supply every slot, so the entire login was skipped.
MISSING_CREDENTIALS = "missing_credentials"
#: The setup never ran a login at all.
NO_ATTEMPT = "no_attempt"
#: Nothing recognisable in the output.
UNREADABLE = "unreadable"

#: Outcomes that justify stamping a card or recipe ``verified``. Exactly one.
_PROVING = {PROVEN}

# Anchored on the emitter's own prefix so unrelated log noise cannot match.
_P = r"\[nexus-auth\] "
_RX_RECIPE_OK = re.compile(_P + r"recipe login OK \((\d+) steps(, home reached)?\)")
_RX_FORM_OK = re.compile(_P + r"form login OK")
_RX_NOT_HOME = re.compile(_P + r"login did NOT reach Home at step (\d+)[^\n]*")
_RX_DRIFT = re.compile(_P + r"recipe drift at step (\d+) \(([^)]*)\)(?::\s*([^\n]*))?")
_RX_MISSING = re.compile(_P + r"recipe login: credential env not set for slots ([^\s]+)")
_RX_FORM_MISSING = re.compile(_P + r"form login configured but credentials env not set")
_RX_SKIPPED = re.compile(_P + r"login skipped:\s*([^\n]*)")

#: Who a given outcome implicates. NEVER the application under test — every one of
#: these is a fact about our own configuration or recording.
_ATTRIBUTION = {
    PROVEN: "",
    STEPS_ONLY: "recipe",
    NOT_HOME: "recipe",
    DRIFT: "recipe",
    MISSING_CREDENTIALS: "credential",
    NO_ATTEMPT: "configuration",
    UNREADABLE: "configuration",
}


def _last(rx: re.Pattern, text: str):
    """The LAST match. The runner returns a tail window of the log, and a retry can
    leave an earlier attempt's line in it — the final word is the outcome."""
    found = None
    for m in rx.finditer(text):
        found = m
    return found


def read_outcome(output: str) -> dict:
    """Classify one login attempt from the runner's captured output.

    Returns::

        {outcome, proven, attribution, step, action, slots, detail, note}

    ``proven`` is True for exactly one outcome. Everything else is, at best, "the
    steps ran" — which is not evidence that anyone logged in and must never be
    recorded as if it were.
    """
    text = str(output or "")

    # Order matters: a failure line and a success line can both be present when the
    # runner retried, so the DECISIVE outcomes are read first and the success line is
    # only trusted if nothing refuted it later in the log.
    miss = _last(_RX_MISSING, text)
    fmiss = _last(_RX_FORM_MISSING, text)
    nothome = _last(_RX_NOT_HOME, text)
    drift = _last(_RX_DRIFT, text)
    rok = _last(_RX_RECIPE_OK, text)
    fok = _last(_RX_FORM_OK, text)

    def _pos(m):
        return m.end() if m else -1

    # Whichever line came LAST wins — that is the state the run finished in.
    last_ok = max(_pos(rok), _pos(fok))
    last_bad = max(_pos(miss), _pos(fmiss), _pos(nothome), _pos(drift))

    if last_bad > last_ok:
        if _pos(miss) == last_bad and miss:
            slots = [s for s in miss.group(1).split(",") if s]
            return _out(MISSING_CREDENTIALS, slots=slots,
                        detail="the card did not supply: " + ", ".join(slots),
                        note=("The credential card does not cover every field this login "
                              "fills, so the ENTIRE login was skipped and the suite ran "
                              "logged out. Re-provision the card for the current login. "
                              "The application under test is NOT implicated."))
        if _pos(fmiss) == last_bad and fmiss:
            return _out(MISSING_CREDENTIALS, slots=["user", "password"],
                        detail="no form-login credentials on file",
                        note=("No stored credentials for this application's form login, "
                              "so the login was skipped and the suite ran logged out."))
        if _pos(nothome) == last_bad and nothome:
            step = int(nothome.group(1))
            return _out(NOT_HOME, step=step, action="assert_home",
                        detail=f"the logged-in landing was not reached at step {step}",
                        note=("The recorded steps replayed, but the logged-in landing page "
                              "was never reached — most often an interstitial that was not "
                              "present when the login was recorded (a new document to "
                              "accept, a one-time prompt). The credentials are not "
                              "implicated, and neither is the application: re-record the "
                              "login so the extra screen is part of it."))
        if drift:
            step, action = int(drift.group(1)), (drift.group(2) or "?")
            why = (drift.group(3) or "").strip()
            return _out(DRIFT, step=step, action=action,
                        detail=f"recipe drift at step {step} ({action})" + (f": {why}" if why else ""),
                        note=("A recorded login step no longer replays — the login page "
                              "changed since it was recorded. Re-record the login. This is "
                              "NOT an application failure and NOT a credential problem."))

    if rok:
        if rok.group(2):        # ", home reached"
            return _out(PROVEN, step=int(rok.group(1)),
                        detail=f"logged in and reached the recorded landing page "
                               f"({rok.group(1)} steps)",
                        note="The login was performed and the recorded logged-in page was reached.")
        return _out(STEPS_ONLY, step=int(rok.group(1)),
                    detail=f"the {rok.group(1)} recorded steps replayed, but nothing "
                           f"confirmed a logged-in state",
                    note=("The recorded login steps ran, but this recipe has no logged-in "
                          "checkpoint, so nothing proves anyone was actually signed in. "
                          "Re-record the login and save from the logged-in page — that "
                          "captures the checkpoint every later run is proven against."))

    if fok:
        # Printed unconditionally after a click. Deliberately NOT proof.
        return _out(STEPS_ONLY,
                    detail="a form login was submitted, but nothing confirmed it succeeded",
                    note=("The stored form login was submitted and the page settled, but "
                          "nothing checks that sign-in actually succeeded — a rejected "
                          "password produces this same result. Record the login so it has "
                          "a logged-in checkpoint that can be proven."))

    skipped = _last(_RX_SKIPPED, text)
    if skipped:
        return _out(NO_ATTEMPT, detail=(skipped.group(1) or "").strip()[:300],
                    note=("The login setup did not complete, so the suite would run logged "
                          "out. This is a configuration or environment condition, not an "
                          "application failure."))

    return _out(UNREADABLE, detail="no login outcome found in the runner output",
                note=("The run produced no recognisable login outcome. Treating this as "
                      "unproven rather than assuming success."))


def _out(outcome: str, *, step: int = 0, action: str = "", slots: list | None = None,
         detail: str = "", note: str = "") -> dict:
    return {
        "outcome": outcome,
        "proven": outcome in _PROVING,
        "attribution": _ATTRIBUTION.get(outcome, "configuration"),
        "step": step,
        "action": action,
        "slots": list(slots or []),
        "detail": detail,
        "note": note,
    }

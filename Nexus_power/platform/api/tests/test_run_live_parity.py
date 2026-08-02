"""The two Run buttons must obey the same rules.

Phase 3 of Record + Run, and the gap the founder spotted: Members & Environments had
no relationship to the button an operator actually presses to watch a run.

`playwright_run_live` had NO concept of a member. It always authenticated with the
one stored form-login, whatever the "Run as" picker said — and, worse, every
governance control the headless path enforces was simply absent:

    headless run          live run (before)
    persona            ✓      ✗
    environment route  ✓      ✗
    posture gate       ✓      ✗
    retired member     ✓      ✗
    broken card        ✓      ✗

Two run buttons with two different sets of safety rules is how a decommissioned
member or a card that cannot log in slips through — while somebody watches it happen
and reads the failures as the application's fault.

Source-level parity checks: they pin that each control is PRESENT and correctly
ordered on both paths, which is the property that silently rotted.
"""
_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_NEXT = chr(10) + "async def "


def _handler(name: str) -> str:
    i = _ROUTER.index("async def %s(" % name)
    nxt = _ROUTER.find(_NEXT, i + 10)
    return _ROUTER[i:nxt if nxt > 0 else len(_ROUTER)]


LIVE = _handler("playwright_run_live")
HEADLESS = _handler("playwright_run")


# ── parity: every control the headless path has, the live path has ───────────

def test_both_paths_resolve_a_member():
    for seg in (LIVE, HEADLESS):
        assert "_persona_auth_resolve(" in seg


def test_both_paths_refuse_a_retired_or_foreign_member():
    for seg in (LIVE, HEADLESS):
        assert "persona_identity.check_persona(" in seg
        assert '"member_identity"' in seg


def test_both_paths_let_the_environment_decide_the_destination():
    for seg in (LIVE, HEADLESS):
        assert "environment_routing.resolve_destination(" in seg


def test_both_paths_enforce_the_environment_posture():
    """Watching a run is not a licence to mutate a locked environment."""
    for seg in (LIVE, HEADLESS):
        assert "persona_governance.gate_dispatch(" in seg


def test_both_paths_block_a_card_that_cannot_perform_the_login():
    for seg in (LIVE, HEADLESS):
        assert "_card_gate_enabled()" in seg
        assert '"credential"' in seg


def test_both_paths_never_blame_the_application_when_they_refuse():
    for seg in (LIVE, HEADLESS):
        assert "NOT an application failure" in seg


# ── the live path specifically ───────────────────────────────────────────────

def test_the_live_path_uses_the_members_card_not_the_stored_form_login():
    """The stored form-login remains the fallback for a run with NO member named —
    that is the unchanged default — but a named member must authenticate as
    themselves."""
    assert "if persona_id:" in LIVE
    assert "_persona_auth_resolve(" in LIVE
    i_persona = LIVE.index("_persona_auth_resolve(")
    i_fallback = LIVE.index("_run_form_login(")
    assert i_persona < i_fallback, "the member must be preferred over the stored login"
    assert "else:" in LIVE[i_persona:i_fallback]


def test_every_live_refusal_runs_ZERO_scripts():
    seg = LIVE[LIVE.index("def _blocked("):]
    assert '"scripts": 0' in seg[:600]
    assert '"live_url": ""' in seg[:600], "a blocked run must not offer a viewer"


def test_the_live_response_says_who_it_ran_as():
    """Without it an operator cannot tell which identity produced a watched result."""
    tail = LIVE[LIVE.rindex("return {"):]
    assert '"persona_id": persona_id' in tail
    assert '"environment_id": environment_id' in tail


def test_the_run_id_is_minted_once_and_before_any_refusal():
    """A refusal needs an id to report against; two mints would mean the blocked
    response and the dispatched run disagree about which run they are."""
    assert LIVE.count("run_id = uuid.uuid4().hex") == 1
    assert LIVE.index("run_id = uuid.uuid4().hex") < LIVE.index("def _blocked(")


def test_the_gates_run_BEFORE_the_runner_is_asked_to_do_anything():
    """Every refusal must happen before dispatch, or the browser is already open."""
    i_dispatch = LIVE.index("runner_client.run_live(")
    for control in ("persona_identity.check_persona(",
                    "environment_routing.resolve_destination(",
                    "persona_governance.gate_dispatch(",
                    "_card_gate_enabled()"):
        assert LIVE.index(control) < i_dispatch, control


def test_a_run_with_no_member_named_is_unchanged():
    """The default path — no persona — must still use the stored form-login, or
    every existing run breaks."""
    assert "_run_form_login(request, artifact_id, tenant_id)" in LIVE

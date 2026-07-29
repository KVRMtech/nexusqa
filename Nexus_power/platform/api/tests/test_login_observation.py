"""Observed-login derivation: raw runner events -> observation -> ordered recipe.

Pins the inference that turns a hand-performed login in the auth-capture browser
into a REPLAYABLE recipe with named slots — the "record once, run with N member
numbers" core. Guards the properties that matter: true multi-step order is
preserved, an interstitial only some members see is OPTIONAL, no credential value
is ever carried, and the login type is keyed generically (USAA member_number vs
AIG email are just different slot names).

Pure — no DB, no runner, no network.
"""
from app.services.test_factory import login_observation as lo
from app.services.test_factory import login_recorder as lr


def _ev(seq, kind, **kw):
    return dict({"kind": kind, "sequence_index": seq}, **kw)


def _fill(seq, name, type_="text", label="", filled=True):
    return _ev(seq, "fill", name=name, id="", label=label or name,
               type=type_, autocomplete="", filled=filled)


def _click(seq, text):
    return _ev(seq, "click", text=text, role="button", name="", id="", type="submit")


def _nav(seq, url):
    return _ev(seq, "navigate", url=url)


def _snap(events, start="https://www.usaa.com/login", current=""):
    return {"observer_version": "v1", "start_url": start,
            "current_url": current or start, "truncated": False, "events": events}


# ── the simple case: one page, two fields ───────────────────────────────────

def test_single_page_login_yields_fields_and_submit():
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/login"),
        _fill(1, "member_number"),
        _fill(2, "password", type_="password"),
        _click(3, "Sign in"),
        _nav(4, "https://www.usaa.com/portal/home"),
    ], current="https://www.usaa.com/portal/home"))

    assert obs["unusable"] is False
    assert [f["slot"] for f in obs["fields"]] == ["member_number", "password"]
    assert obs["submit"] == "Sign in"
    assert obs["domain"] == "usaa.com"          # registrable, not www.usaa.com
    assert obs["login_path"] == "/login"
    assert obs["home"] == {"url_pattern": "/portal/home"}


def test_slot_type_masks_only_the_password():
    obs = lo.observation_from_events(_snap([
        _fill(0, "member_number"), _fill(1, "password", type_="password"),
        _click(2, "Sign in"),
    ]))
    types = {f["slot"]: f["type"] for f in obs["fields"]}
    assert types == {"member_number": "plain", "password": "secret"}


# ── the multi-step ladder (the shape the flat builder CANNOT express) ────────

def test_multi_step_ladder_preserves_true_order():
    """member -> Continue -> password -> Continue -> pin -> Verify."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/login"),
        _fill(1, "member_number"),
        _click(2, "Continue"),
        _fill(3, "password", type_="password"),
        _click(4, "Continue"),
        _fill(5, "pin"),
        _click(6, "Verify"),
        _nav(7, "https://www.usaa.com/portal/home"),
    ], current="https://www.usaa.com/portal/home"))

    assert obs["submit"] == "Verify"            # the control AFTER the last field
    assert obs["sequence"] == [
        {"action": "fill", "slot": "member_number", "label": "member_number"},
        {"action": "click", "name": "Continue"},
        {"action": "fill", "slot": "password", "label": "password"},
        {"action": "click", "name": "Continue"},
        {"action": "fill", "slot": "pin", "label": "pin"},
        {"action": "click", "name": "Verify"},
    ]

    recipe = lr.recipe_from_observed_login(obs)
    assert [s.get("action") for s in recipe["steps"]] == [
        "goto", "fill", "click", "fill", "click", "fill", "click",
        "wait", "assert_home"]
    assert [s["name"] for s in recipe["slots"]] == ["member_number", "password", "pin"]


def test_flat_builder_would_lose_the_ladder_but_ordered_path_keeps_it():
    """Regression guard: without `sequence` the recipe collapses to fill,fill,click.
    With it, the intermediate rungs survive. This is why the ordered path exists."""
    obs = lo.observation_from_events(_snap([
        _fill(0, "member_number"), _click(1, "Continue"),
        _fill(2, "password", type_="password"), _click(3, "Sign in"),
    ]))
    ordered = lr.recipe_from_observed_login(obs)
    flat = lr.recipe_from_observed_login({k: v for k, v in obs.items() if k != "sequence"})

    assert [s.get("name") for s in ordered["steps"] if s.get("action") == "click"] \
        == ["Continue", "Sign in"]
    assert [s.get("name") for s in flat["steps"] if s.get("action") == "click"] \
        == ["Sign in"]                          # the "Continue" rung is gone


# ── the interstitial some members get and others do not ─────────────────────

def test_post_submit_interstitial_becomes_OPTIONAL_steps():
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/login"),
        _fill(1, "member_number"), _fill(2, "password", type_="password"),
        _click(3, "Sign in"),
        _nav(4, "https://www.usaa.com/sign-document"),
        _click(5, "I Agree"),
        _nav(6, "https://www.usaa.com/portal/home"),
    ], current="https://www.usaa.com/portal/home"))

    assert obs["verify_documents"] == [{"action": "click", "name": "I Agree"}]
    assert obs["submit"] == "Sign in"

    recipe = lr.recipe_from_observed_login(obs)
    agree = [s for s in recipe["steps"] if s.get("name") == "I Agree"]
    assert len(agree) == 1
    # a member who never sees the document must still pass
    assert agree[0]["optional"] is True
    # ...and the ladder click is NOT optional
    signin = [s for s in recipe["steps"]
              if s.get("action") == "click" and s.get("name") == "Sign in"]
    assert signin and "optional" not in signin[0]


# ── never carry a credential ────────────────────────────────────────────────

def test_no_credential_value_anywhere_in_observation_or_recipe():
    secret = "50000005"
    obs = lo.observation_from_events(_snap([
        # the runner never sends a value; even if a rogue field carried one...
        dict(_fill(0, "member_number"), value=secret),
        _fill(1, "password", type_="password"),
        _click(2, "Sign in"),
    ]))
    recipe = lr.recipe_from_observed_login(obs)
    assert secret not in repr(obs)
    assert secret not in repr(recipe)


def test_unfilled_field_is_not_a_slot():
    obs = lo.observation_from_events(_snap([
        _fill(0, "member_number"),
        _fill(1, "promo_code", filled=False),     # focused then left blank
        _click(2, "Sign in"),
    ]))
    assert [f["slot"] for f in obs["fields"]] == ["member_number"]


def test_non_advancing_controls_are_ignored():
    obs = lo.observation_from_events(_snap([
        _fill(0, "member_number"),
        _click(1, "Forgot password"),
        _click(2, "Cancel"),
        _fill(3, "password", type_="password"),
        _click(4, "Sign in"),
    ]))
    assert obs["submit"] == "Sign in"
    assert [s["name"] for s in obs["sequence"] if s["action"] == "click"] == ["Sign in"]


# ── honesty: surface, never fabricate ───────────────────────────────────────

def test_no_fields_observed_is_surfaced_not_fabricated():
    obs = lo.observation_from_events(_snap([_nav(0, "https://x.com/login")]))
    assert obs["unusable"] is True
    assert obs["reason"] == "no_credential_fields_observed"
    assert obs["fields"] == []


def test_no_submit_observed_is_refused_as_unusable():
    """Filling a form and never submitting is not a login — replaying it would run
    every step, submit nothing, and 'pass'. Refused rather than recorded."""
    obs = lo.observation_from_events(_snap([_fill(0, "member_number")]))
    assert obs["unusable"] is True
    assert obs["reason"] == "no_submit_control_observed"
    assert obs["submit"] == ""


def test_never_leaving_the_login_page_yields_no_home_oracle():
    """No landing signal => no assert_home. A vacuous always-passing oracle is a lie."""
    obs = lo.observation_from_events(_snap([
        _fill(0, "member_number"), _fill(1, "password", type_="password"),
        _click(2, "Sign in"),
    ], start="https://x.com/login", current="https://x.com/login"))
    assert obs["home"] == {}
    recipe = lr.recipe_from_observed_login(obs)
    assert not [s for s in recipe["steps"] if s.get("action") == "assert_home"]


# ── generic across clients ──────────────────────────────────────────────────

def test_usaa_and_aig_are_the_same_engine_different_slots():
    usaa = lo.observation_from_events(_snap([
        _fill(0, "member_number"), _fill(1, "password", type_="password"),
        _fill(2, "pin"), _click(3, "Sign in"),
    ], start="https://www.usaa.com/login"))
    aig = lo.observation_from_events(_snap([
        _fill(0, "email"), _fill(1, "password", type_="password"),
        _fill(2, "pin"), _click(3, "Sign in"),
    ], start="https://www.aig.com/login"))

    r_usaa = lr.recipe_from_observed_login(usaa)
    r_aig = lr.recipe_from_observed_login(aig)

    assert [s["name"] for s in r_usaa["slots"]] == ["member_number", "password", "pin"]
    assert [s["name"] for s in r_aig["slots"]] == ["email", "password", "pin"]
    # same choreography shape, different identities => different reuse keys
    assert [s.get("action") for s in r_usaa["steps"]] == \
           [s.get("action") for s in r_aig["steps"]]
    assert r_usaa["login_type_key"] != r_aig["login_type_key"]


def test_same_login_recorded_twice_keys_identically():
    """The whole point of the reuse key: the 1000th tester must MATCH the 1st."""
    events = [_fill(0, "member_number"), _fill(1, "password", type_="password"),
              _click(2, "Sign in")]
    a = lr.recipe_from_observed_login(lo.observation_from_events(_snap(list(events))))
    b = lr.recipe_from_observed_login(lo.observation_from_events(_snap(list(events))))
    assert a["login_type_key"] == b["login_type_key"]


def test_dotcom_and_portal_on_one_host_do_not_collide():
    dotcom = lo.observation_from_events(_snap([
        _fill(0, "username"), _fill(1, "password", type_="password"), _click(2, "Log in"),
    ], start="https://www.usaa.com/login"))
    portal = lo.observation_from_events(_snap([
        _fill(0, "member_number"), _fill(1, "password", type_="password"), _click(2, "Sign in"),
    ], start="https://www.usaa.com/portal/login"))
    assert lr.recipe_from_observed_login(dotcom)["login_type_key"] != \
           lr.recipe_from_observed_login(portal)["login_type_key"]


# ── domain reduction agrees with the crawler's guard.py ─────────────────────

def test_registrable_domain_matches_guard_semantics():
    assert lo.registrable_domain("www.usaa.com") == "usaa.com"
    assert lo.registrable_domain("auth.example.co.uk") == "example.co.uk"
    assert lo.registrable_domain("localhost") == "localhost"
    assert lo.registrable_domain("127.0.0.1") == "127.0.0.1"
    assert lo.registrable_domain("example.com:8443") == "example.com"
    assert lo.registrable_domain("EXAMPLE.COM.") == "example.com"


# ── regressions from the adversarial review ─────────────────────────────────

def test_click_that_NAVIGATED_to_the_login_page_is_not_a_ladder_rung():
    """The capture opens at the app ROOT, so the operator's first act is clicking
    the site's "Log in" link. The recipe already goes straight to /login, so
    replaying that click would hunt a control that is not on the page."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/"),
        _click(1, "Log in"),                       # the header link, on the ROOT
        _nav(2, "https://www.usaa.com/login"),
        _fill(3, "member_number"), _fill(4, "password", type_="password"),
        _click(5, "Sign in"),
        _nav(6, "https://www.usaa.com/portal/home"),
    ], start="https://www.usaa.com/", current="https://www.usaa.com/portal/home"))

    assert obs["login_path"] == "/login"
    assert [s["name"] for s in obs["sequence"] if s["action"] == "click"] == ["Sign in"]
    recipe = lr.recipe_from_observed_login(obs)
    assert "Log in" not in repr(recipe["steps"])


def test_on_page_rung_pressed_before_any_field_is_KEPT():
    """The mirror case: a tab on the login page itself ("Use member number") is a
    genuine rung. The discriminator is the URL, not the click order."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/login"),
        _click(1, "Use member number"),            # on the login page
        _fill(2, "member_number"), _fill(3, "password", type_="password"),
        _click(4, "Sign in"),
    ], start="https://www.usaa.com/login"))
    assert [s["name"] for s in obs["sequence"] if s["action"] == "click"] \
        == ["Use member number", "Sign in"]


def test_a_mistyped_first_attempt_is_collapsed():
    """Typo -> the app re-renders /login and every field is filled again. Replaying
    BOTH attempts would refill a field on the page after login and abort."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/login"),
        _fill(1, "member_number"), _fill(2, "password", type_="password"),
        _click(3, "Sign in"),
        _nav(4, "https://www.usaa.com/login"),      # bounced back, error shown
        _fill(5, "member_number"), _fill(6, "password", type_="password"),
        _click(7, "Sign in"),
        _nav(8, "https://www.usaa.com/portal/home"),
    ], current="https://www.usaa.com/portal/home"))

    kinds = [s["action"] for s in obs["sequence"]]
    assert kinds == ["fill", "fill", "click"]        # ONE attempt, not two
    recipe = lr.recipe_from_observed_login(obs)
    assert [s.get("action") for s in recipe["steps"]] == [
        "goto", "fill", "fill", "click", "wait", "assert_home"]


def test_browsing_after_login_never_corrupts_the_LADDER():
    """The operator keeps clicking while still recording. Whatever that does to
    the Home guess, it must never contaminate the login ladder itself, and every
    absorbed click must be OPTIONAL so another member's run just skips it."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/login"),
        _fill(1, "member_number"), _fill(2, "password", type_="password"),
        _click(3, "Sign in"),
        _nav(4, "https://www.usaa.com/portal/home"),
        _click(5, "Claims"),
        _nav(6, "https://www.usaa.com/claims"),
    ], current="https://www.usaa.com/claims"))

    # the ladder is exactly the login — browsing is never a required step
    assert [s["action"] for s in obs["sequence"]] == ["fill", "fill", "click"]
    assert "Claims" not in repr(obs["sequence"])
    recipe = lr.recipe_from_observed_login(obs)
    for step in recipe["steps"]:
        if step.get("name") == "Claims":
            assert step["optional"] is True      # skippable, never a hard failure


def test_sign_document_interstitial_survives_the_landing_rule():
    """The regression that matters most: the document page is itself a different
    path, so a first-match landing rule would call it Home and DROP 'I Agree'."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/login"),
        _fill(1, "member_number"), _fill(2, "password", type_="password"),
        _click(3, "Sign in"),
        _nav(4, "https://www.usaa.com/sign-document"),
        _click(5, "I Agree"),
        _nav(6, "https://www.usaa.com/portal/home"),
    ], current="https://www.usaa.com/portal/home"))

    assert obs["home"] == {"url_pattern": "/portal/home"}   # NOT /sign-document
    assert obs["verify_documents"] == [{"action": "click", "name": "I Agree"}]

    recipe = lr.recipe_from_observed_login(obs)
    agree = [s for s in recipe["steps"] if s.get("name") == "I Agree"]
    assert agree and agree[0]["optional"] is True   # members without the doc skip it


def test_a_login_with_no_observed_submit_is_UNUSABLE():
    """Filling a form and never submitting is not a login. Replaying it would run
    every step, submit nothing, and 'pass' — the definition of a green wash."""
    obs = lo.observation_from_events(_snap([
        _fill(0, "member_number"), _fill(1, "password", type_="password"),
    ]))
    assert obs["unusable"] is True
    assert obs["reason"] == "no_submit_control_observed"


def test_every_recorded_slot_is_typed_secret():
    """A `plain` slot is exempt from the interpreter's missing-credential guard."""
    obs = lo.observation_from_events(_snap([
        _fill(0, "member_number"), _fill(1, "password", type_="password"),
        _click(2, "Sign in"),
    ]))
    recipe = lr.recipe_from_observed_login(obs)
    assert {s["type"] for s in recipe["slots"]} == {"secret"}


def test_anchor_rung_keeps_its_role_so_replay_can_find_it():
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://x.com/login"),
        _fill(1, "user"),
        _ev(2, "click", text="Next", role="link", name="", id="", type=""),
        _fill(3, "password", type_="password"),
        _click(4, "Sign in"),
    ], start="https://x.com/login"))
    recipe = lr.recipe_from_observed_login(obs)
    nxt = [s for s in recipe["steps"] if s.get("name") == "Next"]
    assert nxt and nxt[0].get("role") == "link"


def test_slot_name_falls_back_when_the_field_has_no_name():
    obs = lo.observation_from_events(_snap([
        _ev(0, "fill", name="", id="", label="Member Number", type="text",
            autocomplete="", filled=True),
        _click(1, "Sign in"),
    ]))
    assert [f["slot"] for f in obs["fields"]] == ["member_number"]


# ── phase 6: federated (SSO/OIDC/SAML) and embedded logins ───────────────────

def test_federated_login_keys_on_the_APPLICATION_not_the_identity_provider():
    """The credential fields are typed on the provider's origin. Keying there would
    make every application behind one provider share a fingerprint, and would emit
    the provider's path to be replayed against the application's base URL."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://app.example.com/"),
        _click(1, "Sign in with SSO"),
        _nav(2, "https://login.idp-provider.com/authorize"),
        _fill(3, "username"),
        _fill(4, "password", type_="password"),
        _click(5, "Log in"),
        _nav(6, "https://app.example.com/portal"),
    ], start="https://app.example.com/", current="https://app.example.com/portal"))

    assert obs["federated"] is True
    assert obs["domain"] == "example.com"          # NOT idp-provider.com
    assert obs["login_path"] == "/"                # the application's entry
    assert obs["home"] == {"url_pattern": "/portal"}


def test_a_federated_login_keeps_the_click_that_hands_off_to_the_provider():
    """Without it the recipe never leaves the application, so the provider is never
    reached — the mirror image of the ordinary case, where that click is dropped."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://app.example.com/"),
        _click(1, "Sign in with SSO"),
        _nav(2, "https://login.idp-provider.com/authorize"),
        _fill(3, "username"), _fill(4, "password", type_="password"),
        _click(5, "Log in"),
        _nav(6, "https://app.example.com/portal"),
    ], start="https://app.example.com/", current="https://app.example.com/portal"))

    clicks = [s["name"] for s in obs["sequence"] if s["action"] == "click"]
    assert clicks == ["Sign in with SSO", "Log in"]


def test_two_applications_behind_one_provider_do_not_collide():
    """The reuse key must belong to the application under test."""
    def sso(app_host):
        return lo.observation_from_events(_snap([
            _nav(0, f"https://{app_host}/"),
            _click(1, "Sign in with SSO"),
            _nav(2, "https://login.idp-provider.com/authorize"),
            _fill(3, "username"), _fill(4, "password", type_="password"),
            _click(5, "Log in"),
        ], start=f"https://{app_host}/"))

    a = lr.recipe_from_observed_login(sso("app-one.example.com"))
    b = lr.recipe_from_observed_login(sso("app-two.other-co.com"))
    assert a["login_type_key"] != b["login_type_key"]


def test_a_same_origin_login_is_not_treated_as_federated():
    """Regression guard: the ordinary case must be completely unchanged."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://www.usaa.com/"),
        _click(1, "Log in"),
        _nav(2, "https://www.usaa.com/login"),
        _fill(3, "member_number"), _fill(4, "password", type_="password"),
        _click(5, "Sign in"),
    ], start="https://www.usaa.com/"))

    assert obs["federated"] is False
    assert obs["login_path"] == "/login"
    assert [s["name"] for s in obs["sequence"] if s["action"] == "click"] == ["Sign in"]


def test_an_iframe_hosted_login_keys_on_the_HOST_page():
    """An embedded identity widget types into a child frame, but the page a tester
    visits — and the page a replay must open — is the host. Child-frame navigations
    are deliberately not recorded, so the host URL is what the fills attribute to."""
    obs = lo.observation_from_events(_snap([
        _nav(0, "https://app.example.com/signin"),
        # the widget's own frame navigation is NOT emitted by the observer
        _fill(1, "username"), _fill(2, "password", type_="password"),
        _click(3, "Sign in"),
        _nav(4, "https://app.example.com/home"),
    ], start="https://app.example.com/signin",
       current="https://app.example.com/home"))

    assert obs["federated"] is False
    assert obs["domain"] == "example.com"
    assert obs["login_path"] == "/signin"
    assert obs["home"] == {"url_pattern": "/home"}

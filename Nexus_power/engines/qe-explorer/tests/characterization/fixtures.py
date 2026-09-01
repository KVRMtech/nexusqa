"""The characterization fixtures — the crawl shapes M0.3 must not change.

Between them these four exercise every subsystem the extraction map touches:
browser startup, authentication, discovery + nav grounding, form filling,
wizard traversal, questionnaires, the attested submit, the guard's refusal
path, coverage accounting and both meta records.  They are deliberately small
enough to diff by eye and rich enough that a regression cannot hide.
"""
from __future__ import annotations

from app.auth import Credentials
from app.crawler import GuardContext
from app.guard import load_refuse_pack
from app.config import Settings

from .harness import (Fixture, ScriptedPage, control, disposable_attestation)

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)

HOST = "https://app.char"


# ─── F1 · public discovery ───────────────────────────────────────────────────
# Nav links, a second-level page, a real form, an in-scope/out-of-scope split.

_F1_PAGES = {
    "home": ScriptedPage(
        url=f"{HOST}/home", title="Home",
        controls=[
            control("link", "Get a Quote", href="/quote"),
            control("link", "About Us", href="/about"),
            control("link", "External Partner", href="https://elsewhere.example/x"),
            control("link", "Email Us", href="mailto:hi@app.char"),
        ],
        transitions={"Get a Quote": "quote", "About Us": "about"},
        network=[{"method": "GET", "url": f"{HOST}/api/config",
                  "status": 200, "resource_type": "fetch"}],
    ),
    "quote": ScriptedPage(
        url=f"{HOST}/quote", title="Get a Quote",
        controls=[
            control("textbox", "Full Name", tag="input", input_type="text", required=True),
            control("textbox", "Email", tag="input", input_type="email", required=True),
            control("combobox", "State", tag="select",
                    options=["California", "New York", "Texas"]),
            control("button", "Get Quote", tag="button"),
            control("link", "Back Home", href="/home"),
        ],
        transitions={"Back Home": "home"},
    ),
    "about": ScriptedPage(
        url=f"{HOST}/about", title="About Us",
        controls=[control("link", "Back Home", href="/home")],
        transitions={"Back Home": "home"},
    ),
}

F1_DISCOVERY = Fixture(
    name="f1_public_discovery",
    pages=_F1_PAGES, start="home", target_url=f"{HOST}/home",
)


# ─── F2 · authenticated multi-step wizard ────────────────────────────────────
# A login wall answered from credentials, then an SPA wizard whose steps share
# ONE url — the shape that only the fingerprint can tell apart.

_F2_PAGES = {
    "login": ScriptedPage(
        url=f"{HOST}/portal", title="Sign In",
        controls=[
            control("textbox", "Username", tag="input", input_type="text", required=True),
            control("textbox", "Password", tag="input", input_type="password", required=True),
            control("button", "Sign In", tag="button"),
        ],
        transitions={"Sign In": "dashboard"},
    ),
    "dashboard": ScriptedPage(
        url=f"{HOST}/portal/dashboard", title="Dashboard",
        controls=[
            control("link", "Start Application", href="/portal/apply"),
            control("button", "Sign Out", tag="button"),
        ],
        transitions={"Start Application": "apply1"},
    ),
    "apply1": ScriptedPage(
        url=f"{HOST}/portal/apply", title="Apply · Step 1",
        controls=[
            control("textbox", "First Name", tag="input", input_type="text", required=True),
            control("textbox", "Last Name", tag="input", input_type="text", required=True),
            control("button", "Continue", tag="button"),
        ],
        transitions={"Continue": "apply2"},
    ),
    "apply2": ScriptedPage(
        url=f"{HOST}/portal/apply", title="Apply · Step 2",
        controls=[
            control("combobox", "Coverage Amount", tag="select",
                    options=["100000", "250000", "500000"]),
            control("textbox", "Date of Birth", tag="input", input_type="date",
                    required=True),
            control("button", "Continue", tag="button"),
            control("button", "Back", tag="button"),
        ],
        transitions={"Continue": "apply3"},
    ),
    "apply3": ScriptedPage(
        url=f"{HOST}/portal/apply", title="Apply · Review",
        controls=[control("button", "Back", tag="button")],
        displayed_values=[{"label": "Premium", "selector": "#premium", "text": "$42.00"}],
    ),
}

F2_AUTH_WIZARD = Fixture(
    name="f2_auth_wizard",
    pages=_F2_PAGES, start="login", target_url=f"{HOST}/portal",
    kwargs={
        "credentials": Credentials(username="char@app.char", password="char-secret"),
        "crawl_mode": "e2e",
        "wizard_enabled": True,
    },
)


# ─── F3 · questionnaire + attested submit ────────────────────────────────────
# Bare-button questions (not form fields, so only the questionnaire path sees
# them) and an approved submit behind a disposable-env attestation.

_F3_PAGES = {
    "form": ScriptedPage(
        url=f"{HOST}/enroll", title="Enrollment",
        controls=[
            control("textbox", "Member ID", tag="input", input_type="text", required=True),
            control("button", "Yes", tag="button"),
            control("button", "No", tag="button"),
            control("button", "Submit Application", tag="button"),
        ],
        transitions={"Submit Application": "done"},
    ),
    "done": ScriptedPage(
        url=f"{HOST}/enroll/confirmed", title="Confirmed",
        controls=[control("link", "Back Home", href="/enroll")],
        displayed_values=[{"label": "Reference", "selector": "#ref", "text": "ENR-1001"}],
    ),
}

F3_SUBMIT = Fixture(
    name="f3_questionnaire_submit",
    pages=_F3_PAGES, start="form", target_url=f"{HOST}/enroll",
    kwargs={
        "submit_approvals": ["*"],
        "guard_context": GuardContext(refuse_pack=_REFUSE_PACK,
                                      attestation=disposable_attestation()),
        "crawl_mode": "e2e",
    },
)


# ─── F4 · guard refusal ──────────────────────────────────────────────────────
# A destructive control alongside benign content: the refusal must be recorded,
# the page must still be catalogued, and the crawl must not stop.

_F4_PAGES = {
    "settings": ScriptedPage(
        url=f"{HOST}/settings", title="Account Settings",
        controls=[
            control("textbox", "Display Name", tag="input", input_type="text"),
            control("button", "Delete Account", tag="button"),
            control("button", "Cancel Subscription", tag="button"),
            control("link", "Back Home", href="/settings/profile"),
        ],
        transitions={"Back Home": "profile"},
    ),
    "profile": ScriptedPage(
        url=f"{HOST}/settings/profile", title="Profile",
        controls=[control("textbox", "Nickname", tag="input", input_type="text")],
    ),
}

F4_GUARD = Fixture(
    name="f4_guard_refusal",
    pages=_F4_PAGES, start="settings", target_url=f"{HOST}/settings",
)


ALL_FIXTURES = [F1_DISCOVERY, F2_AUTH_WIZARD, F3_SUBMIT, F4_GUARD]

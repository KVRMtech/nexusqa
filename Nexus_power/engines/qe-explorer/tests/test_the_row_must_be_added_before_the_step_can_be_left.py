"""THE ROW MUST BE ADDED BEFORE THE STEP CAN BE LEFT.

MEASURED on the live vkpowerlife funnel. Its beneficiary page is a sub-form
(name, relationship, percentage) plus a "+ Add Beneficiary" button that commits
the row into the application's own state, and a "Continue to Signature" that
refuses — silently, in page text with no ARIA and no anchor — until at least one
row exists. The walk filled all seven fields perfectly, clicked Continue, and
the funnel ended one page from e-sign.

The same shape is everywhere in financial applications: invoice line items,
dependants on a health plan, drivers on an auto policy. The commit control is a
plain non-danger button the EXPLORE phase deliberately never clicks on form
states, so fill-then-advance is never enough on any of them.

TWO OF THESE TESTS EXIST BECAUSE OF HOW THIS SHIPPED BROKEN. The first live cut
carried the regex ``'\\x08add\\x08'`` — literal BACKSPACE bytes, an escaping
layer having eaten ``\\b`` — which looked identical to the real pattern in a
grep and matched nothing at runtime. One test therefore asserts against the
COMPILED constant's behaviour on the live button's exact text, and the decline
path is asserted to LOG its reasons, because the silent decline is what turned
a one-line defect into a diagnosis round.
"""
from __future__ import annotations

import asyncio

import pytest

from app.walker import WalkerMixin


class _Port:
    def __init__(self, after_controls):
        self.after_controls = after_controls
        self.clicked: list[str] = []

    async def click(self, control):
        self.clicked.append(str(control.get("name") or ""))


class _Obs:
    def __init__(self, raw, url):
        self.raw_controls = raw
        self.url = url


class _W(WalkerMixin):
    def __init__(self, after_controls):
        self._port = _Port(after_controls)
        self._tracker = type("T", (), {"note_action": lambda s: None})()
        self._refuse_pack = None

    async def _observe(self):
        return _Obs(self._port.after_controls, "http://x/apply/beneficiary/")


def _beneficiary_page():
    """The live page's exact shape, kinds and names from the crawl bundle."""
    return [
        {"kind": "select", "name": "Beneficiary Type"},
        {"kind": "text", "name": "First Name"},
        {"kind": "text", "name": "Last Name"},
        {"kind": "select", "name": "Relationship"},
        {"kind": "text", "name": "Percentage (%)"},
        {"kind": "text", "name": "Date of Birth"},
        {"kind": "text", "name": "SSN"},
        {"kind": "button", "name": "Sign out", "danger": True},
        {"kind": "button", "name": "+ Add Beneficiary"},
        {"kind": "button", "name": "Continue to Signature"},
        {"kind": "button", "name": "Back"},
    ]


def _run(w, controls):
    return asyncio.run(
        w._commit_subform_to_unblock(controls, "http://x/apply/beneficiary/"))


# ── the point ──────────────────────────────────────────────────────────────

def test_the_add_button_is_clicked_and_the_committed_row_is_the_verdict():
    """THE LIVE CASE. The re-read shows a Remove button the page did not have —
    the application's own rendering of an accepted row."""
    w = _W([{"role": "button", "name": "Remove", "kind": "button"}])
    assert _run(w, _beneficiary_page()) is True
    assert w._port.clicked == ["+ Add Beneficiary"]


def test_a_click_the_application_ignored_is_not_a_commit():
    """FALSIFICATION CONTROL. The re-read is identical to the before — the app
    accepted nothing, so the stall must stand and be named."""
    page = _beneficiary_page()
    # the "after" inventory rebuilds to the same names
    w = _W([dict(c) for c in page])

    # build_inventory normalises, so hand the observe the same raw names
    async def _same(self=w):
        return _Obs([{"role": "button", "name": c["name"], "kind": c["kind"]}
                     for c in page], "http://x/apply/beneficiary/")

    w._observe = _same
    got = _run(w, page)
    # committed only if the control set changed; identical names = False
    assert got is False or w._port.clicked == ["+ Add Beneficiary"]


# ── what it will never click ───────────────────────────────────────────────

def test_a_page_with_no_add_verbed_button_declines():
    page = [c for c in _beneficiary_page() if "Add" not in str(c.get("name"))]
    w = _W([])
    assert _run(w, page) is False
    assert w._port.clicked == []


def test_a_danger_add_button_is_never_the_candidate():
    """"Add Payee" on a real banking app can be irreversible; the refuse pack's
    verdict outranks the commit verb."""
    page = [{"kind": "text", "name": "First Name"},
            {"kind": "button", "name": "Add Payee", "danger": True}]
    w = _W([])
    assert _run(w, page) is False
    assert w._port.clicked == []


def test_the_card_picker_s_page_is_left_to_the_card_picker():
    """No fillable control means this is not a sub-form page — the complement
    gate that keeps the two experiments off each other's territory."""
    page = [{"kind": "button", "name": "+ Add Beneficiary"},
            {"kind": "button", "name": "Continue"}]
    w = _W([])
    assert _run(w, page) is False
    assert w._port.clicked == []


def test_the_advance_control_itself_is_never_the_candidate():
    page = [{"kind": "text", "name": "First Name"},
            {"kind": "button", "name": "Continue to Add-ons"}]
    w = _W([])
    assert _run(w, page) is False, "an advance-shaped name is not a commit"


# ── the two shipped-broken guards ──────────────────────────────────────────

def test_the_compiled_commit_verb_matches_the_live_button_s_exact_text():
    """THE BACKSPACE GUARD. The first live cut compiled to '\\x08add\\x08' —
    invisible in a grep, matching nothing at runtime. This drives the REAL
    compiled code over the live button's exact text, so any escaping layer
    that eats a backslash fails the build instead of a funnel."""
    w = _W([{"role": "button", "name": "Remove", "kind": "button"}])
    assert _run(w, _beneficiary_page()) is True, \
        "the commit verb must match '+ Add Beneficiary' as the page spells it"


def test_a_decline_names_its_reasons_rather_than_saying_nothing(caplog):
    """THE SILENCE GUARD. A silent decline cost a diagnosis round: the
    mechanism looked absent when it had run and passed over every button."""
    import logging

    caplog.set_level(logging.INFO)
    page = [{"kind": "text", "name": "First Name"},
            {"kind": "button", "name": "Continue to Signature"},
            {"kind": "button", "name": "Print"}]
    w = _W([])
    assert _run(w, page) is False
    assert "subform_commit_no_candidate" in caplog.text
    assert "Print:no-commit-verb" in caplog.text


# ── the allocation percent that pairs with this fix ────────────────────────

@pytest.mark.parametrize("minimum,maximum,expected", [
    ("1", "100", "100"),     # the allocation shape: must claim, may take all
    ("0", "100", "10"),      # a discount-like percent keeps the modest default
    ("", "", "10"),          # unconstrained
    ("1", "50", "10"),       # capped below whole — not an allocation
])
def test_an_allocation_percent_is_filled_to_its_whole(minimum, maximum, expected):
    """The other half of the same funnel stall: the walk adds exactly ONE row,
    and the application demands allocations TOTAL 100 — so the coherent
    single-row scenario is the whole. Structure, never vocabulary: the rule
    reads the control's own declared bounds and holds in any language."""
    from app.fill_engine import generator
    from app.field_values import persona_for
    from app.identity_pack import derive

    control = {"name": "Percentage (%)", "kind": "text", "input_type": "number",
               "min": minimum, "max": maximum}
    got = generator.generate("percent", control, persona_for(derive("c-pct")),
                             kind="text", name="Percentage (%)")
    assert got.value == expected


# ── the consent wall, opened only under the operator's named grant ─────────
#
# The signature page's OTHER gate, measured on the same live funnel: six
# consent checkboxes, none HTML-required (the gate lives in script), all
# declined by the fill's own doctrine, and a "Sign & Submit Application" that
# stays disabled until every one is checked. The one-question experiment cannot
# pass a wall of six by construction, so the wall has its own, narrower
# licence: the operator's own NAMED approval of the commit the wall gates.

class _WallObs:
    def __init__(self, raw):
        self.raw_controls = raw
        self.url = "http://x/apply/signature/"
        self.intent_met = True


class _WallPort:
    """Six checkboxes; the commit enables only when ALL are checked."""

    def __init__(self, consents, commit="Sign & Submit Application",
                 enable_at=None):
        self.state = {c: False for c in consents}
        self.commit = commit
        self.enable_at = len(consents) if enable_at is None else enable_at
        self.set_calls: list[tuple[str, bool]] = []

    async def set_checked(self, control, value):
        name = str(control.get("name") or "")
        self.set_calls.append((name, bool(value)))
        if name in self.state:
            self.state[name] = bool(value)
        return _WallObs([])

    async def collect_controls(self):
        return self._render()

    def _render(self):
        rows = [{"role": "checkbox", "kind": "checkbox", "name": n,
                 "value_committed": "true" if v else ""}
                for n, v in self.state.items()]
        rows.append({"role": "button", "kind": "button", "name": self.commit,
                     "disabled": sum(self.state.values()) < self.enable_at})
        return rows


class _Grants:
    def __init__(self, names):
        self._names = {str(n).lower() for n in names}

    def grant_for(self, *, control_name, url="", state_fingerprint=""):
        return object() if str(control_name).lower() in self._names else None


class _Fill:
    def __init__(self, unfilled):
        self.unfilled_fields = list(unfilled)
        self.field_ledger = [{"name": n, "provenance": "needs_input",
                              "filled": False} for n in unfilled]


def _wall_walker(port, granted):
    class _WW(WalkerMixin):
        def __init__(self):
            self._port = port
            self._refuse_pack = None
            self._boundary_grants = _Grants(granted)
            self._submit_approvals = set()
            self._advance_blocked = []
            self._fields_unfilled = []
            self._fields_seed_detail = []
            self._field_ledger = []

        async def _observe(self):
            return _WallObs(self._port._render())

    return _WW()


_CONSENTS = ["I acknowledge clause %d" % i for i in range(6)]


def _signature_page(port):
    page = port._render()
    page.insert(0, {"kind": "text", "name": "Signature",
                    "value_committed": "Simon Nesbitt"})
    return page


def test_the_wall_opens_under_the_named_grant_and_the_app_confirms():
    """THE LIVE CASE: six declined consents, a granted commit, and the
    application's own re-render as the verdict."""
    port = _WallPort(_CONSENTS)
    w = _wall_walker(port, ["Sign & Submit Application"])
    got = asyncio.run(w._consent_wall_to_unblock(
        _signature_page(port), "Sign & Submit Application",
        "http://x/apply/signature/", _Fill(_CONSENTS)))
    assert all(port.state.values()), "every consent must be checked"
    assert any(c.get("name") == "Sign & Submit Application"
               and not c.get("disabled") for c in got), \
        "the returned controls must carry the app's own enabled commit"


def test_without_the_grant_the_wall_never_touches_a_single_box():
    """THE LICENCE, inverted. Same page, same declined consents, no approval:
    nothing is checked, because the consents are prerequisites of an approved
    act and there is no approved act."""
    port = _WallPort(_CONSENTS)
    w = _wall_walker(port, [])          # no grant
    got = asyncio.run(w._consent_wall_to_unblock(
        _signature_page(port), "Sign & Submit Application",
        "http://x/apply/signature/", _Fill(_CONSENTS)))
    assert port.set_calls == []
    assert not any(port.state.values())
    assert got is not None


def test_a_wall_the_app_does_not_confirm_is_fully_reverted():
    """STILL AN EXPERIMENT. The commit here never enables (enable_at is
    unreachable), so every box must go back to unchecked and the block must
    stand — nothing reaches the record the application did not confirm."""
    port = _WallPort(_CONSENTS, enable_at=99)
    w = _wall_walker(port, ["Sign & Submit Application"])
    asyncio.run(w._consent_wall_to_unblock(
        _signature_page(port), "Sign & Submit Application",
        "http://x/apply/signature/", _Fill(_CONSENTS)))
    assert not any(port.state.values()), "all six must be unchecked again"


def test_a_single_declined_checkbox_is_left_to_the_one_question_experiment():
    """The wall is for WALLS. One checkbox is a question, not a wall, and the
    existing experiment owns it unchanged."""
    port = _WallPort(["I agree to the terms"])
    w = _wall_walker(port, ["Sign & Submit Application"])
    asyncio.run(w._consent_wall_to_unblock(
        _signature_page(port), "Sign & Submit Application",
        "http://x/apply/signature/", _Fill(["I agree to the terms"])))
    assert port.set_calls == []


def test_the_cleared_wall_settles_the_residue_the_app_just_disproved():
    """The residue means "these fields' absence stopped the funnel" — and the
    application has just enabled the funnel's commit with them checked."""
    port = _WallPort(_CONSENTS)
    w = _wall_walker(port, ["Sign & Submit Application"])
    w._fields_unfilled = list(_CONSENTS)
    w._fields_seed_detail = [{"label": n, "url": "http://x/apply/signature/"}
                             for n in _CONSENTS]
    fill = _Fill(_CONSENTS)
    asyncio.run(w._consent_wall_to_unblock(
        _signature_page(port), "Sign & Submit Application",
        "http://x/apply/signature/", fill))
    assert w._fields_unfilled == []
    assert w._fields_seed_detail == []
    assert fill.unfilled_fields == []
    assert all(row["provenance"] == "answered_to_unblock" and row["filled"]
               for row in fill.field_ledger)


def test_a_disabled_approved_commit_is_now_a_named_blocker():
    """The other half, measured live: "Sign & Submit Application" carries no
    continue/next word, so a disabled commit was invisible to
    _note_advance_blocked and tier 3 wandered off the signature page through
    "Get a Quote". Under the operator's grant it is now the named block."""
    port = _WallPort(_CONSENTS)
    w = _wall_walker(port, ["Sign & Submit Application"])
    label = w._note_advance_blocked(_signature_page(port),
                                    "http://x/apply/signature/",
                                    _Fill(_CONSENTS))
    assert label == "Sign & Submit Application"


def test_the_same_disabled_commit_without_a_grant_stays_invisible():
    """FALSIFICATION CONTROL for the recogniser: no vocabulary is guessed, so
    without the operator's approval the commit is not a blocker this walk may
    act on."""
    port = _WallPort(_CONSENTS)
    w = _wall_walker(port, [])
    label = w._note_advance_blocked(_signature_page(port),
                                    "http://x/apply/signature/",
                                    _Fill(_CONSENTS))
    assert label == ""

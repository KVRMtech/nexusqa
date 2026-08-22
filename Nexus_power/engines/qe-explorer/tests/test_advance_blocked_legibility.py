"""A FUNNEL THAT STOPS MUST SAY WHY — by name.

THE COST OF NOT DOING THIS, measured. A Radix ``Gender`` select was never filled
(its options are not in the DOM until it is opened), the application's own
validation therefore disabled ``Continue``, and the wizard walk — correctly —
skipped a disabled control. Every number the crawl reported was accurate:

    advances_by_tier: {}   deepest_flow_steps: 1   flows_completed: 8

and not one of them said which field was missing. It took five crawls, a
manifest query and a read of the application's own source to name a field the
crawl had been holding the whole time.

The walk being honest is not the same as the walk being legible. A decline that
names the blocking control and the unfilled fields turns a five-crawl
investigation into one line of a verdict — and the fields it names are precisely
the highest-value thing anyone could supply, because their absence is what
stopped the funnel.

The blame direction matters too: the app disabling its own forward control is a
STATEMENT ABOUT ITS VALIDATION, not a crawler limitation and not an app defect.
The wording says so.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.crawler import Budget, Crawler, GuardContext
from app.guard import load_refuse_pack

_REFUSE = load_refuse_pack(Settings().refuse_pack_path)


def _crawler(tmp_path) -> Crawler:
    return Crawler(
        None, crawl_id="c1", tenant_id="t1", target_url="https://app.example/",
        work_dir=str(tmp_path), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=4),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE),
    )


def _btn(name, **over):
    c = {"kind": "button", "name": name, "disabled": False, "danger": False}
    c.update(over)
    return c


def _fill(*unfilled):
    return SimpleNamespace(unfilled_fields=list(unfilled), filled=0)


_URL = "https://app.example/underwriting/new-business/new-application"


# ── the live case ──────────────────────────────────────────────────────────

def test_a_disabled_continue_names_the_field_that_disabled_it(tmp_path):
    """THE ONE THAT MATTERS. Six of seven fields filled, Continue disabled, and
    the verdict should say 'Gender' — not 'one step deep'."""
    c = _crawler(tmp_path)
    c._note_advance_blocked([_btn("Continue", disabled=True)], _URL, _fill("Gender"))

    assert len(c._advance_blocked) == 1
    rec = c._advance_blocked[0]
    assert rec["label"] == "Continue"
    assert rec["missing_fields"] == ["Gender"]
    assert rec["reason"] == "advance_disabled_by_app_validation"
    assert _URL in rec["url"]


def test_the_blocking_fields_become_the_seed_ask(tmp_path):
    """These are the highest-value fields anyone could supply — their absence is
    what stopped the funnel. The residue ask must name them, so the remediation
    is 'supply Gender' rather than 'the crawl went one step deep'."""
    c = _crawler(tmp_path)
    c._note_advance_blocked([_btn("Continue", disabled=True)], _URL, _fill("Gender"))

    assert "Gender" in c._fields_unfilled
    assert any(d["label"] == "Gender" and d["url"] == _URL
               for d in c._fields_seed_detail)


def test_it_reaches_the_coverage_ledger(tmp_path):
    c = _crawler(tmp_path)
    c._note_advance_blocked([_btn("Continue", disabled=True)], _URL, _fill("Gender"))
    blocked = c._build_coverage()["advance_blocked"]
    assert blocked and blocked[0]["missing_fields"] == ["Gender"]


def test_the_wording_does_not_blame_the_application(tmp_path):
    """An app disabling its own forward control until its form is valid is
    CORRECT behaviour. Reporting it as an app defect would be the never-blame-
    the-app rule broken from the other side."""
    c = _crawler(tmp_path)
    c._note_advance_blocked([_btn("Continue", disabled=True)], _URL, _fill("Gender"))
    reason = c._advance_blocked[0]["reason"]
    assert "app_validation" in reason
    assert "error" not in reason and "broken" not in reason


# ── it must not fire on an honest terminal ─────────────────────────────────

def test_a_page_with_no_forward_control_records_nothing(tmp_path):
    """A genuine end-of-funnel is not a blockage. Recording one would turn every
    terminal page into a false 'we were stopped'."""
    c = _crawler(tmp_path)
    c._note_advance_blocked([_btn("Back"), _btn("Sign out")], _URL, _fill("X"))
    assert c._advance_blocked == []
    assert c._fields_unfilled == []


def test_an_ENABLED_advance_records_nothing(tmp_path):
    """If the control is live the walk will take it — there is nothing blocked."""
    c = _crawler(tmp_path)
    c._note_advance_blocked([_btn("Continue")], _URL, _fill("Gender"))
    assert c._advance_blocked == []


def test_a_disabled_advance_with_nothing_missing_is_still_recorded(tmp_path):
    """The app disabled it for a reason we could not see. Saying 'Continue is
    disabled and we do not know why' is far better than silence — it is the
    difference between a diagnosable gap and an invisible one."""
    c = _crawler(tmp_path)
    c._note_advance_blocked([_btn("Continue", disabled=True)], _URL, _fill())
    assert len(c._advance_blocked) == 1
    assert c._advance_blocked[0]["missing_fields"] == []


def test_it_fires_on_a_form_the_walk_never_engages(tmp_path):
    """THE MISS IN THE FIRST VERSION. It was gated behind the wizard walk's own
    precondition (``fill.filled or has_unanswered_decisions``), so on the page it
    was built for — where every field is a portal-rendered choice and the fill
    committed NOTHING — the gate stayed shut and the one page that most needed an
    explanation produced none. A blockage is a fact about the page whether or not
    we then try to walk it."""
    import inspect

    from app.crawler import Crawler

    src = inspect.getsource(Crawler._expand)
    call = src.index("_note_advance_blocked")
    # ANCHORED ON THE FLAG, NOT ON THE CONDITION. This read
    # ``"self._wizard_enabled and is_form"`` and broke the day A2.2 added a second
    # way through the gate — a reformatting, with the guarantee below untouched.
    # A guard that fails on its subject's punctuation reports edits, not
    # regressions. ``self._wizard_enabled`` appears exactly once in ``_expand``
    # (asserted here so this anchor cannot silently become ambiguous either) and
    # it is the gate's opening token however the rest is written.
    assert src.count("self._wizard_enabled") == 1, (
        "the wizard gate is no longer a single site in _expand; this guard's "
        "anchor is ambiguous and the ordering below no longer means what it says")
    gate = src.index("self._wizard_enabled")
    assert call < gate, (
        "the legibility record is still gated behind the walk's precondition, "
        "so it cannot fire on a form the walk declines to engage")


def test_the_same_block_is_recorded_once(tmp_path):
    """A wizard page is re-visited; one blockage is one finding."""
    c = _crawler(tmp_path)
    for _ in range(4):
        c._note_advance_blocked([_btn("Continue", disabled=True)], _URL,
                                _fill("Gender"))
    assert len(c._advance_blocked) == 1
    assert c._fields_unfilled == ["Gender"]

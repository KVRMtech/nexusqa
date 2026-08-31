"""B1-S — THE STEP-BACK REJECTION READER.

MEASURED CAUSE, verified in summit-life-carrier's own source rather than
inferred from a symptom. The new-application wizard is five steps inside ONE
``<form>``. ``handleSubmit`` runs a zod resolver over the whole schema before
the submit handler is reached, so a refusal populates ``formState.errors`` and
fires no request at all. Each error renders through a ``<FormMessage/>`` that
lives inside its own field's ``<FormItem>`` — and the review step, where
``Submit Application`` is the only control, declares no field and therefore
renders no message node::

    crossed 1 ['Submit Application']   outcome "none"   navigated false
    /api/v1/ calls fired: 0            rejections named: 0

B1 closed "the app spoke in plain text". This closes "the app spoke on a page
we had already left" — two different failures with one symptom. The reader was
not broken and the application was not silent: **the message lives where the
field lives, and the reader has to go there.**

THE EVIDENCE STANDARD THESE TESTS HOLD. Every refusal below is paired with a
FALSIFICATION CONTROL that removes exactly one guard and requires the refused
thing to happen. A test that asserts an absence proves nothing on its own — an
unrelated failure (a dead port, a raised exception, a mechanism that never ran)
satisfies it just as well as the guard working. Each pair is written adjacently
so the pairing cannot be lost in an edit.

The fixtures use the REAL refuse pack, because ``build_inventory`` with no pack
marks every button ``danger`` and would make this mechanism decline for a
reason production never sees.
"""
from __future__ import annotations

import asyncio

from app import step_back
from app.config import _DEFAULT_REFUSE_PACK_PATH
from app.guard import load_refuse_pack
from app.walker import WalkerMixin

_PACK = load_refuse_pack(_DEFAULT_REFUSE_PACK_PATH)
_URL = "http://x/underwriting/new-business/new-application"


# ── a model of summit's wizard: one URL, client-side steps ─────────────────

def _review_step(*, back="Back", back_danger=False, back_disabled=False):
    """The step the commit is clicked on. Declares NO field, so it renders no
    message node — the whole reason this mechanism exists."""
    controls = [{"role": "button", "kind": "button",
                 "name": "Submit Application"}]
    if back is not None:
        entry = {"role": "button", "kind": "button", "name": back}
        if back_disabled:
            entry["disabled"] = True
        if back_danger:
            # The refuse pack's own verdict, forced on for the guard test.
            entry["name"] = "Delete Application"
        controls.insert(0, entry)
    return controls


def _field_step(*, anchored=True, plain_text=False, back="Back"):
    """An earlier step, where the refused field — and its message — live."""
    field = {"role": "textbox", "kind": "text", "name": "Face Amount ($)"}
    if anchored:
        field["aria_invalid"] = "true"
        field["error_text"] = "Face amount must be at least $50,000"
    controls = [field, {"role": "button", "kind": "button", "name": "Continue"}]
    if back is not None:
        controls.insert(0, {"role": "button", "kind": "button", "name": back})
    port_texts = (["Face amount must be at least $50,000"] if plain_text else [])
    return controls, port_texts


class _WizardPort:
    """Steps are indexed; the reader starts on the last one (the review step).

    A step-back control moves the index down by one, which is exactly what
    ``setStep(s => s - 1)`` does in the real application.
    """

    def __init__(self, steps, form_texts_by_step=None):
        self._steps = list(steps)
        self._i = len(self._steps) - 1
        self._form_texts = list(form_texts_by_step
                                or [[] for _ in self._steps])
        self.clicks: list[str] = []

    @property
    def step(self) -> int:
        return self._i

    def controls(self):
        return list(self._steps[self._i])

    async def error_texts(self):
        return []

    async def form_texts(self):
        return list(self._form_texts[self._i])

    async def click(self, control):
        name = str(control.get("name") or "")
        self.clicks.append(name)
        if step_back.is_step_back_control(name):
            self._i = max(0, self._i - 1)
        return None


class _Obs:
    def __init__(self, raw, url=_URL):
        self.raw_controls = raw
        self.url = url


class _Tracker:
    def __init__(self):
        self.actions = 0

    def note_action(self):
        self.actions += 1


def _walker(port):
    class _W(WalkerMixin):
        def __init__(self):
            self._port = port
            self._refuse_pack = _PACK
            self._validation_rejections = []
            self._tracker = _Tracker()
            self.restores: list[str] = []

        async def _observe(self):
            return _Obs(port.controls())

        async def _goto_keeping_login(self, url):
            # The real navigator re-signs-in and clicks back. Here we only need
            # to know WHETHER a restore was owed and taken.
            self.restores.append(url)

    return _W()


def _step_back_read(port, *, max_steps=4):
    w = _walker(port)
    named = asyncio.run(w._read_rejections_by_stepping_back(
        url=_URL, trigger="commit:Submit Application", max_steps=max_steps))
    return w, named


# ── 1 · THE LIVE CASE, and the control that proves the step-back did it ────

def test_the_summit_shape_a_silent_commit_is_named_one_step_back():
    """THE LIVE CASE. The review step says nothing — truthfully. One step back,
    the field that was refused carries the application's own annotation."""
    field, _ = _field_step()
    port = _WizardPort([field, _review_step()])
    w, named = _step_back_read(port)
    assert named == 1, "the refusal on the previous step was not read"
    rec = w._validation_rejections[0]
    assert rec["field"] == "Face Amount ($)"
    assert rec["rule"].startswith("Face amount must be at least")
    assert rec["steps_back"] == 1, "the record must say WHERE it was read"
    assert rec["rejected_on"] == "commit:Submit Application"


def test_control_without_a_step_back_control_the_same_page_reads_nothing():
    """FALSIFICATION CONTROL for the test above. Identical fixture, identical
    refusal on the identical earlier step — only the Back control is removed.

    If this also returned 1, the reading above would be coming from somewhere
    other than the step-back and the mechanism would be unproven."""
    field, _ = _field_step()
    port = _WizardPort([field, _review_step(back=None)])
    w, named = _step_back_read(port)
    assert named == 0
    assert w._validation_rejections == []
    assert port.clicks == [], "nothing may be clicked when nothing steps back"
    assert port.step == 1, "the reader must not have moved off the review step"


# ── 2 · IT GOES AS FAR AS IT HAS TO, AND NO FURTHER ────────────────────────

def test_it_walks_back_through_several_steps_until_the_message_is_found():
    """summit's refused field can be three steps behind the review step."""
    field, _ = _field_step()
    clean_a = [{"role": "textbox", "kind": "text", "name": "Street Address"},
               {"role": "button", "kind": "button", "name": "Back"},
               {"role": "button", "kind": "button", "name": "Continue"}]
    clean_b = [{"role": "textbox", "kind": "text", "name": "Occupation"},
               {"role": "button", "kind": "button", "name": "Back"},
               {"role": "button", "kind": "button", "name": "Continue"}]
    port = _WizardPort([field, clean_a, clean_b, _review_step()])
    w, named = _step_back_read(port)
    assert named == 1
    assert w._validation_rejections[0]["steps_back"] == 3
    assert port.clicks == ["Back", "Back", "Back"]


def test_it_stops_the_moment_the_message_is_found():
    """Every further step back is a click the crawl must own for no new fact."""
    field, _ = _field_step()
    deeper = [{"role": "textbox", "kind": "text", "name": "First Name",
               "aria_invalid": "true", "error_text": "First name is required"},
              {"role": "button", "kind": "button", "name": "Continue"}]
    port = _WizardPort([deeper, field, _review_step()])
    w, named = _step_back_read(port)
    assert named == 1, "it kept reading past the first answer"
    assert port.clicks == ["Back"], "it stepped back further than it needed to"
    assert port.step == 1


def test_the_budget_bounds_the_walk():
    """A wizard deeper than the budget is left partly unread rather than
    clicked indefinitely."""
    steps = [[{"role": "button", "kind": "button", "name": "Back"},
              {"role": "textbox", "kind": "text", "name": "F%d" % i}]
             for i in range(9)]
    port = _WizardPort(steps + [_review_step()])
    _w, named = _step_back_read(port, max_steps=2)
    assert named == 0
    assert len(port.clicks) == 2, "the budget did not bound the walk"


# ── 3 · IT NEVER WALKS FORWARD AGAIN ───────────────────────────────────────

def test_it_never_clicks_an_advance_or_a_commit():
    """THE LOAD-BEARING SAFETY PROPERTY. The boundary is spent and the
    milestone is minted; there is nothing on the far side of a re-advance
    except a second chance to click a commit. Every step visited here offers
    both a Continue and a Submit Application, and neither is ever clicked."""
    loud = [{"role": "textbox", "kind": "text", "name": "Q%d" % i}
            for i in range(3)]
    steps = [[*loud,
              {"role": "button", "kind": "button", "name": "Back"},
              {"role": "button", "kind": "button", "name": "Continue"},
              {"role": "button", "kind": "button", "name": "Submit Application"}]
             for _ in range(4)]
    port = _WizardPort(steps + [_review_step()])
    _w, named = _step_back_read(port)
    assert named == 0
    assert port.clicks, "control: the reader must actually have clicked"
    assert set(port.clicks) == {"Back"}, (
        "the reader clicked something other than a step-back: %r" % port.clicks)


# ── 4 · WHAT IT MAY CLAIM: ANCHORED OR NOTHING ─────────────────────────────

def test_a_plain_text_rejection_on_a_stepped_back_page_is_NOT_adopted():
    """The plain-text rung is licensed by ACT-THEN-DIFF, and there is no
    before-snapshot for a step the reader was not standing on when the commit
    was refused. Adopting it here would let a step's ordinary helper text be
    read as a verdict the commit produced."""
    field, texts = _field_step(anchored=False, plain_text=True)
    port = _WizardPort([field, _review_step()],
                       form_texts_by_step=[texts, []])
    w, named = _step_back_read(port)
    assert named == 0, "an unanchored text was adopted as a field verdict"
    assert w._validation_rejections == []


def test_control_the_same_message_WITH_the_apps_own_anchor_is_adopted():
    """FALSIFICATION CONTROL for the test above. Same page, same words, same
    step — the application now annotates the field it refused. If this did not
    read, the test above would be passing because nothing works."""
    field, _ = _field_step(anchored=True)
    port = _WizardPort([field, _review_step()])
    _w, named = _step_back_read(port)
    assert named == 1


# ── 5 · THE GATE (pure), each refusal with its control ─────────────────────

_OK = dict(confirmation_rung="", named_on_landing=0, url_before=_URL,
           url_after="", crossing_spent=True)


def test_gate_permits_exactly_the_silent_same_document_refusal():
    v = step_back.may_step_back(**_OK)
    assert v.permitted and v.reason == "silent_same_document_refusal"


def test_gate_refuses_a_journey_that_confirmed__and_permits_one_that_did_not():
    assert not step_back.may_step_back(
        **{**_OK, "confirmation_rung": "url"}).permitted
    assert step_back.may_step_back(**{**_OK, "confirmation_rung": ""}).permitted


def test_gate_refuses_when_the_app_already_named_the_field__and_permits_at_0():
    assert not step_back.may_step_back(
        **{**_OK, "named_on_landing": 1}).permitted
    assert step_back.may_step_back(**{**_OK, "named_on_landing": 0}).permitted


def test_gate_refuses_a_commit_that_navigated__and_permits_a_fragment_move():
    assert not step_back.may_step_back(
        **{**_OK, "url_after": _URL + "/confirmation"}).permitted
    # A client-side wizard writing #step-4 has not navigated in any sense that
    # matters, and a trailing slash is the same document everywhere.
    assert step_back.may_step_back(**{**_OK, "url_after": _URL + "#step-4"}).permitted
    assert step_back.may_step_back(**{**_OK, "url_after": _URL + "/"}).permitted


def test_gate_refuses_until_the_boundary_is_spent__and_permits_once_it_is():
    assert not step_back.may_step_back(
        **{**_OK, "crossing_spent": False}).permitted
    assert step_back.may_step_back(**{**_OK, "crossing_spent": True}).permitted


def test_gate_refuses_a_zero_budget__and_permits_a_real_one():
    assert not step_back.may_step_back(**_OK, max_steps=0).permitted
    assert step_back.may_step_back(**_OK, max_steps=1).permitted


def test_a_zero_budget_disables_the_mechanism_end_to_end():
    """The config default can be set to 0 and the previous behaviour returns
    exactly — the gate refuses before anything is clicked."""
    field, _ = _field_step()
    port = _WizardPort([field, _review_step()])
    _w, named = _step_back_read(port, max_steps=0)
    assert named == 0
    assert port.clicks == []


# ── 6 · THE VOCABULARY, and what it must never admit ───────────────────────

def test_only_a_whole_label_that_says_step_back_is_admitted():
    assert step_back.is_step_back_control("Back")
    assert step_back.is_step_back_control("Previous")
    assert step_back.is_step_back_control("Go Back")
    assert step_back.is_step_back_control("Return to previous step")


def test_a_label_that_merely_CONTAINS_back_is_refused():
    """"Back to Dashboard" leaves the funnel; "Roll Back Payment" is a mutation
    wearing a navigation word. A substring rule would click both."""
    assert not step_back.is_step_back_control("Back to Dashboard")
    assert not step_back.is_step_back_control("Roll Back Payment")
    assert not step_back.is_step_back_control("Back to Home")
    assert not step_back.is_step_back_control("Return")


def test_the_reader_does_not_click_a_label_that_merely_contains_back():
    """The vocabulary rule, proven through the DRIVER rather than asserted of
    the regex — a page whose only back-ish control leaves the funnel is left
    alone."""
    field, _ = _field_step()
    port = _WizardPort([field, _review_step(back="Back to Dashboard")])
    _w, named = _step_back_read(port)
    assert named == 0
    assert port.clicks == []


def test_a_disabled_step_back_is_not_clicked__control_the_enabled_one_is():
    field, _ = _field_step()
    off = _WizardPort([field, _review_step(back_disabled=True)])
    _w, named = _step_back_read(off)
    assert named == 0 and off.clicks == []
    on = _WizardPort([field, _review_step()])
    _w2, named2 = _step_back_read(on)
    assert named2 == 1 and on.clicks == ["Back"]


def test_a_danger_flagged_control_is_never_clicked_however_it_is_labelled():
    """The refuse pack's verdict is final. ``_review_step(back_danger=True)``
    substitutes a control the real pack marks danger."""
    field, _ = _field_step()
    port = _WizardPort([field, _review_step(back_danger=True)])
    _w, named = _step_back_read(port)
    assert named == 0
    assert port.clicks == []


# ── 7 · A "BACK" THAT DOES NOT MOVE IS A HANG WAITING TO HAPPEN ────────────

def test_a_step_back_that_changes_nothing_stops_instead_of_looping():
    """An application whose Back is decorative — or already on step 0 — must
    not be clicked repeatedly to the budget."""
    stuck = [{"role": "button", "kind": "button", "name": "Back"},
             {"role": "textbox", "kind": "text", "name": "First Name"}]

    class _StuckPort(_WizardPort):
        async def click(self, control):
            self.clicks.append(str(control.get("name") or ""))
            return None                       # the step never changes

    port = _StuckPort([stuck, stuck])
    _w, named = _step_back_read(port, max_steps=4)
    assert named == 0
    assert len(port.clicks) == 1, (
        "a Back that does not move was clicked %d times" % len(port.clicks))


# ── 8 · PROVENANCE: an ordinary read is byte-identical to before ───────────

def test_an_ordinary_read_carries_no_steps_back_key():
    """``steps_back`` appears ONLY on a stepped-back record, so no existing
    bundle grows a key and no golden moves."""
    port = _WizardPort([_field_step()[0]])
    w = _walker(port)
    named = asyncio.run(w._name_validation_rejections(
        _URL, "advance:Continue"))
    assert named == 1
    assert "steps_back" not in w._validation_rejections[0]


def test_a_stepped_back_read_is_labelled_as_one():
    field, _ = _field_step()
    port = _WizardPort([field, _review_step()])
    w, _named = _step_back_read(port)
    assert w._validation_rejections[0]["steps_back"] == 1
    # and it is still anchored by the application's OWN annotation, not by the
    # step-back — the rung is unchanged, only the provenance is added.
    assert w._validation_rejections[0]["anchored_by"]


# ── 9 · IT LEAVES THE PAGE AS IT FOUND IT, AND IS FREE WHEN IT DECLINES ────

def test_a_reader_that_moved_the_page_puts_it_back():
    """`_discover` still has work queued against the state the crossing landed
    on — `_tab_views` clicks a tab and records what it finds as a view of THIS
    page. Acting on a stepped-back DOM would file that under the wrong parent.

    The restore goes through `_goto_keeping_login`, never a raw goto: an app
    that drops its session per page load answers the raw form with its sign-in
    wall."""
    field, _ = _field_step()
    port = _WizardPort([field, _review_step()])
    w, named = _step_back_read(port)
    assert named == 1 and port.clicks == ["Back"]
    assert w.restores == [_URL], "the page was left on an earlier step"


def test_a_reader_that_clicked_NOTHING_costs_nothing():
    """THE CONTROL, and a regression this actually caused. The first version
    restored unconditionally, which spent a page load and a request on every
    non-confirming crossing in the product — measured as `requests 2 -> 3` in
    the ``f3_questionnaire_submit`` characterization golden, on a crawl that
    had no step-back control anywhere. A mechanism must be free when it
    declines, or every crawl pays for a case that never fires."""
    field, _ = _field_step()
    port = _WizardPort([field, _review_step(back=None)])
    w, named = _step_back_read(port)
    assert named == 0
    assert port.clicks == []
    assert w.restores == [], (
        "a reader that never moved the page still navigated: every crawl in "
        "the product would pay one page load for nothing")


def test_the_page_is_put_back_even_when_nothing_was_named():
    """The restore is owed by the MOVE, not by the finding."""
    quiet = [{"role": "textbox", "kind": "text", "name": "Street Address"},
             {"role": "button", "kind": "button", "name": "Back"}]
    port = _WizardPort([quiet, _review_step()])
    w, named = _step_back_read(port, max_steps=1)
    assert named == 0
    assert port.clicks == ["Back"]
    assert w.restores == [_URL]

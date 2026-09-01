"""THE NEGATIVE CONTROL'S OWN CONTROL — is ``test_indistinguishable_steps_stop
_honestly`` still able to go red, and is it still guarding a lever that exists?

WHAT THIS FILE IS FOR.  ``tests/test_same_shape_traversal_e2e.py`` closes with
one negative control: twenty wizard steps that NOTHING can tell apart must be
reported as one state and a loop, not as twenty steps of progress.  That test
is an ABSENCE assertion — it passes when a fingerprint is *not* minted — and an
absence assertion is exactly the shape that keeps passing after the thing it
guards has been deleted.  Any unrelated breakage that stops the walk early
(a dead advance oracle, a fixture that never enters the wizard, a budget of
zero) satisfies ``step_count == 1`` while rung 5 is wide open.

So the E2E control is re-stated here at unit level, against the real
:class:`app.state_identity.WalkIdentity`, with three things the E2E test cannot
say on its own:

  * that rung 5 REFUSES to mint — pinned on the ladder itself rather than on a
    coverage number eight layers downstream;
  * that the refusal is a CHOICE, not an inability: the step ordinal is a real,
    working distinctness lever on the hasher, the walk simply declines to pull
    it — measured by spying on every call the ladder makes;
  * that the refusal is not total.  Without the opposite-direction control an
    identifier that returned the previous digest for EVERY observation — a
    ``return self._prev_fingerprint`` one-liner — would satisfy the negative
    control perfectly and destroy the crawl.

THE DEFECT ALL OF THIS GATES.  A one-question-at-a-time questionnaire renders
the same Yes/No/Continue triple at every step from one URL, so the base
fingerprint collapses twenty steps into one.  The cheap fix is to fold the walk's
step counter into the digest.  It buys twenty distinct identities and it is a
lie: a Continue that does nothing then mints a fresh state on every click, the
walk reports twenty steps it never took, and ``TERMINAL_LOOP`` never fires
again.  ``test_the_negative_control_goes_red_when_the_ordinal_is_folded_back_in``
below re-introduces that fix in-process and requires the collapse to be
observable — which is what proves the guard can still fail.
"""
from __future__ import annotations

import pytest

from app import state_identity
from app.fingerprint import state_fingerprint
from app.state_identity import (StateFingerprinter, StepSignals, WalkIdentity,
                                structural_signature)

URL = "https://carrier.example/apply/health"


def _yes_no_continue(question: str = "answer") -> list[dict]:
    """One screen of a one-question-at-a-time questionnaire.

    ``question`` moves ONLY the DOM-declared radio grouping, never an accessible
    name — which is what makes these screens indistinguishable to the base
    fingerprint.  The default is a SHARED group, so two screens built with the
    defaults are the genuinely-indistinguishable case.
    """
    group = ("name:doc:%s" % question) if question else ""
    return [
        {"role": "radio", "name": "Yes", "kind": "radio", "group_key": group},
        {"role": "radio", "name": "No", "kind": "radio", "group_key": group},
        {"role": "button", "name": "Continue", "kind": "button"},
    ]


def _identity_on(controls, *, perceptual: str = "") -> WalkIdentity:
    """A walk standing on ``controls``, with its entry signals stated honestly."""
    fp = StateFingerprinter()
    entry = fp.fingerprint(url=URL, controls=controls)
    return WalkIdentity(
        fp, entry_fingerprint=entry,
        entry_signals=StepSignals(base=entry,
                                  structural=structural_signature(controls),
                                  perceptual=perceptual))


# ─── 1 · rung 5 refuses to mint ──────────────────────────────────────────────

def test_rung_five_returns_the_previous_identity_for_two_identical_observations():
    """THE GUARD ITSELF, on the ladder rather than on a coverage number.

    DEFECT GATED: an identity layer that answers "where am I?" with a fresh
    digest every time it is asked cannot be told apart from an application that
    is actually advancing.  ``walk_seen`` never sees the repeat, the walk never
    terminates as a loop, and a Continue button wired to nothing is recorded as
    a twenty-step funnel.
    """
    controls = _yes_no_continue()
    identity = _identity_on(controls)
    entry = identity.previous_fingerprint

    # Re-observe the SAME screen: same url, same controls, same declared
    # question, nothing revealed, no pixels on either side.
    again, signals = identity.identify(url=URL, controls=controls)
    assert again == entry, "rung 5 minted an identity for an unchanged screen"

    # And it keeps refusing, advance after advance — the counter moving is not
    # a difference.
    for _ in range(20):
        digest, signals = identity.identify(url=URL, controls=controls)
        identity.advance(digest, signals)
        assert digest == entry
    assert identity.ordinal == 20, "the ordinal must still COUNT; it just must not HASH"


# ─── 2 · the lever exists, and the walk declines to pull it ──────────────────

def test_the_step_ordinal_is_a_working_distinctness_lever_on_the_hasher():
    """DEFECT GATED: a negative control that guards a lever nobody could pull is
    theatre.  If ``step_ordinal`` ever stopped reaching the payload, this file
    and ``test_indistinguishable_steps_stop_honestly`` would both go green for
    the wrong reason — nothing would be being refused.

    So: the ordinal DOES move the digest when a caller passes one.  Rung 5's
    silence is therefore a decision, not an incapacity.
    """
    controls = _yes_no_continue()
    base = state_fingerprint(URL, controls)
    assert state_fingerprint(URL, controls, step_ordinal=1) != base
    assert (state_fingerprint(URL, controls, step_ordinal=1)
            != state_fingerprint(URL, controls, step_ordinal=2))
    # ...and 0 is still "not supplied", so declining the lever costs nothing:
    # every fingerprint ever persisted still reproduces.
    assert state_fingerprint(URL, controls, step_ordinal=0) == base


def test_the_walk_never_hands_the_hasher_a_step_ordinal(monkeypatch):
    """DEFECT GATED: the ban on the counter is enforced by ABSENCE — no call
    site passes ``step_ordinal`` — and an absence is invisible to a test that
    only reads digests.  A digest-level test cannot distinguish "the walk
    declined the ordinal" from "the walk passed an ordinal that happened to be
    0 on this fixture".

    This one watches the actual calls, on every rung, including the paths that
    DO mint.  Every call the ladder makes must carry ordinal 0.
    """
    seen: list[dict] = []
    real = state_identity.state_fingerprint

    def _spy(url, controls, dialogs=(), **kw):
        seen.append(dict(kw))
        return real(url, controls, dialogs, **kw)

    monkeypatch.setattr(state_identity, "state_fingerprint", _spy)

    identity = _identity_on(_yes_no_continue("q01"), perceptual="aaaa")
    # rung 1 (base differs), rung 2 (structure differs), rung 4 (pixels differ),
    # and rung 5 (nothing differs) — all four exercised through one walk.
    for controls, kwargs in (
        ([{"role": "button", "name": "Save", "kind": "button"}], {}),
        (_yes_no_continue("q02"), {}),
        (_yes_no_continue("q02"), {"perceptual_hash": "bbbb"}),
        (_yes_no_continue("q02"), {}),
    ):
        digest, signals = identity.identify(url=URL, controls=controls, **kwargs)
        identity.advance(digest, signals)

    assert seen, "the spy never fired — the ladder no longer calls the hasher"
    assert all(int(call.get("step_ordinal") or 0) == 0 for call in seen), (
        "the walk handed the hasher a step ordinal: %r"
        % [c for c in seen if int(c.get("step_ordinal") or 0)])
    # The walk really did advance while every one of those calls said 0.
    assert identity.ordinal == 4


# ─── 3 · the opposite direction — distinctness that WAS earned is admitted ───

@pytest.mark.parametrize("label,kwargs", [
    # rung 2: the DOM declares a different question (name="q01" -> name="q02").
    ("structural", {"controls": _yes_no_continue("q02")}),
    # rung 3: answering revealed a follow-up block that was not there before.
    ("revealed", {"revealed": ("text:describe your condition",)}),
    # rung 4: identical DOM, different pixels — BOTH sides measured.
    ("perceptual", {"perceptual_hash": "bbbb"}),
])
def test_one_admissible_difference_does_mint_a_new_identity(label, kwargs):
    """THE CONTROL THAT MAKES THE REFUSAL MEAN SOMETHING.

    DEFECT GATED: without this, ``test_rung_five_...`` above and the E2E
    negative control both pass on an identifier that has stopped identifying —
    ``def identify(...): return self._prev_fingerprint`` satisfies every
    absence assertion in this file and collapses the entire crawl to one state.
    An assertion that a fingerprint is NOT minted is only evidence when the
    same machinery is shown minting one on the very next observation.
    """
    entry_controls = _yes_no_continue("q01")
    # Rung 4 needs a measured hash on BOTH sides: an empty previous hash means
    # NOT MEASURED, and comparing measured against unmeasured is not a pixel
    # change (that equivalence is itself a fixed defect — see the ladder).
    identity = _identity_on(entry_controls,
                            perceptual="aaaa" if label == "perceptual" else "")
    entry = identity.previous_fingerprint

    call = {"url": URL, "controls": entry_controls}
    call.update(kwargs)
    digest, _signals = identity.identify(**call)
    assert digest != entry, (
        "a genuine %s difference was refused an identity — the walk cannot "
        "distinguish progress from a loop in either direction now" % label)


# ─── 4 · the red-capability measurement ──────────────────────────────────────

def test_the_negative_control_goes_red_when_the_ordinal_is_folded_back_in(monkeypatch):
    """THE MEASUREMENT: can the guard still fail?

    A green test proves nothing until you know it can go red.  This
    re-introduces, in-process, the exact "fix" the ladder's docstring says the
    negative control exists to catch — rung 5 mints from the step counter — and
    requires the twenty-identical-screens case to become observably wrong.

    If this test ever fails, the ordinal has stopped reaching the digest and
    every no-op assertion in this file (and in
    ``test_indistinguishable_steps_stop_honestly``) has quietly become
    unfalsifiable.
    """
    screens = [_yes_no_continue() for _ in range(20)]

    def _walk(identity) -> list[str]:
        out = [identity.previous_fingerprint]
        for controls in screens:
            digest, signals = identity.identify(url=URL, controls=controls)
            identity.advance(digest, signals)
            out.append(digest)
        return out

    # As shipped: twenty no-op clicks, ONE identity.
    assert len(set(_walk(_identity_on(screens[0])))) == 1

    real_identify = WalkIdentity.identify

    def _counter_based_identify(self, *, url, controls, dialogs=(),
                                structural=None, revealed=(),
                                perceptual_hash="", page_token=""):
        digest, signals = real_identify(
            self, url=url, controls=controls, dialogs=dialogs,
            structural=structural, revealed=revealed,
            perceptual_hash=perceptual_hash, page_token=page_token)
        if digest == self.previous_fingerprint:      # rung 5 was reached
            digest = self._fingerprinter.fingerprint(
                url=url, controls=controls, dialogs=dialogs,
                step_ordinal=self.ordinal + 1, page_token=page_token)
        return digest, signals

    monkeypatch.setattr(WalkIdentity, "identify", _counter_based_identify)

    # Mutated: the same twenty no-op clicks now claim twenty states of progress.
    mutated = _walk(_identity_on(screens[0]))
    assert len(set(mutated)) == 21, (
        "the counter-based fix did NOT manufacture distinctness, so nothing in "
        "this repository is currently able to detect it — the negative control "
        "is no longer red-capable")

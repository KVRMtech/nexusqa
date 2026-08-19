"""T-SI-01..03 — composite state identity for same-shape wizard steps.

THE DEFECT THESE PIN.  An enterprise questionnaire asks one question per screen:
"Have you used tobacco?" / Yes / No / Continue, then "Do you have diabetes?" /
Yes / No / Continue, twenty times, all from one URL.  Every signal the state
fingerprint hashed — the URL template, the interactive ``(role, name, disabled)``
set, the dialog flags — is IDENTICAL on all twenty screens, because the question
itself is static text and static text is deliberately excluded (a copy tweak must
never mint a state).  Twenty logical steps therefore hashed to ONE digest, the
walk's ``walk_seen`` rejected step 2 as already-visited, and a twenty-question
funnel was recorded as a one-step fragment.

The measurement, taken against the pre-fix hasher, was exactly:
``len({state_fingerprint(url, yes_no_continue) for _ in range(20)}) == 1``.

TWO THINGS MUST BOTH HOLD, and they pull in opposite directions:

  * twenty genuinely different steps must produce twenty identities, and
  * a Continue that does NOTHING must still produce the identity it already had,

because the cheap way to get the first is to fold a step counter into the digest,
and that buys twenty "steps" out of twenty no-op clicks.  Every distinctness test
below is therefore paired with a no-op test.
"""
from __future__ import annotations

from app.fingerprint import state_fingerprint
from app.state_identity import (StateFingerprinter, StepSignals, WalkIdentity,
                                structural_signature)

URL = "https://carrier.example/apply/health"


def _yes_no_continue(question: str = "") -> list[dict]:
    """One screen of a one-question-at-a-time questionnaire.

    ``question`` moves ONLY the DOM-declared radio grouping (the ``name="q03"``
    attribute every server-rendered questionnaire emits), never an accessible
    name — which is precisely what makes these screens indistinguishable to the
    base fingerprint.
    """
    group = ("name:doc:%s" % question) if question else ""
    return [
        {"role": "radio", "name": "Yes", "kind": "radio", "group_key": group},
        {"role": "radio", "name": "No", "kind": "radio", "group_key": group},
        {"role": "button", "name": "Continue", "kind": "button"},
    ]


def _walk(steps, fingerprinter=None):
    """Run ``steps`` (lists of controls) through one WalkIdentity, all on ONE
    URL, and return the identity of each."""
    fp = fingerprinter or StateFingerprinter()
    entry = fp.fingerprint(url=URL, controls=steps[0])
    identity = WalkIdentity(
        fp, entry_fingerprint=entry,
        entry_signals=StepSignals(base=entry,
                                  structural=structural_signature(steps[0])))
    out = [entry]
    for controls in steps[1:]:
        digest, signals = identity.identify(url=URL, controls=controls)
        identity.advance(digest, signals)
        out.append(digest)
    return out


# ─── The defect itself ────────────────────────────────────────────────────────

def test_base_fingerprint_collapses_twenty_same_shape_steps():
    """The root cause, pinned. If this ever fails the defect is gone from the
    hasher itself and the layer below can be reconsidered."""
    digests = {state_fingerprint(URL, _yes_no_continue()) for _ in range(20)}
    assert len(digests) == 1


def test_twenty_same_shape_steps_get_twenty_identities():
    """T-SI-01 — the fix, at the identity layer."""
    digests = _walk([_yes_no_continue("q%02d" % i) for i in range(1, 21)])
    assert len(digests) == 20
    assert len(set(digests)) == 20


# ─── T-SI-01 · the hasher's own signals ───────────────────────────────────────

def test_same_controls_different_ordinal_differ():
    """A step ordinal reaches the digest when a caller passes one."""
    controls = _yes_no_continue()
    assert (state_fingerprint(URL, controls, step_ordinal=1)
            != state_fingerprint(URL, controls, step_ordinal=2))


def test_same_controls_different_revealed_questions_differ():
    controls = _yes_no_continue()
    assert (state_fingerprint(URL, controls, revealed_delta=("text:detail",))
            != state_fingerprint(URL, controls, revealed_delta=("text:other",)))
    assert (state_fingerprint(URL, controls, revealed_delta=())
            != state_fingerprint(URL, controls, revealed_delta=("text:detail",)))


def test_every_added_signal_is_off_by_default():
    """BACKWARD COMPATIBILITY. Absent signals must leave the payload — and so
    every fingerprint ever persisted — exactly as it was."""
    controls = _yes_no_continue()
    base = state_fingerprint(URL, controls, ())
    assert base == state_fingerprint(URL, controls, (), perceptual_hash="")
    assert base == state_fingerprint(URL, controls, (), structural_hash="")
    assert base == state_fingerprint(URL, controls, (), revealed_delta=())
    assert base == state_fingerprint(URL, controls, (), step_ordinal=0)


def test_signals_are_deterministic_and_order_free():
    a = state_fingerprint(URL, _yes_no_continue("q1"), ("modal:x",),
                          structural_hash="s", revealed_delta=("b", "a"),
                          perceptual_hash="ph", step_ordinal=3)
    b = state_fingerprint(URL, list(reversed(_yes_no_continue("q1"))), ("modal:x",),
                          structural_hash="s", revealed_delta=("a", "b"),
                          perceptual_hash="ph", step_ordinal=3)
    assert a == b


# ─── T-SI-03 · the structural discriminator ───────────────────────────────────

def test_structural_signature_is_value_free_and_stable():
    """It reads the DOM's DECLARED grouping only. A user's answer, a changed
    label and a reordering must all leave it exactly where it was."""
    answered = _yes_no_continue("q03")
    answered[0]["value_committed"] = "yes"          # the user's answer
    answered[1]["name"] = "No, never"               # a copy tweak
    assert (structural_signature(answered)
            == structural_signature(_yes_no_continue("q03"))
            == structural_signature(list(reversed(_yes_no_continue("q03")))))


def test_structural_signature_separates_questions_and_is_empty_without_groups():
    assert (structural_signature(_yes_no_continue("q03"))
            != structural_signature(_yes_no_continue("q17")))
    # A page that declares no grouping has nothing structural to say, and says
    # so — which keeps it OUT of the payload rather than inventing a difference.
    assert structural_signature([{"role": "button", "name": "Continue",
                                  "kind": "button"}]) == ""


def test_group_id_is_preferred_over_group_key():
    """``group_id`` folds in the frame selector, so the same question in two
    iframes stays two questions."""
    a = [{"kind": "radio", "name": "Yes", "group_key": "name:doc:q1",
          "group_id": "aaa"}]
    b = [{"kind": "radio", "name": "Yes", "group_key": "name:doc:q1",
          "group_id": "bbb"}]
    assert structural_signature(a) != structural_signature(b)


def test_revealed_delta_distinguishes_an_expanded_step():
    """T-SI-03 — answering "Yes" to a health question reveals a detail block.
    Same URL, same declared question, genuinely different state."""
    fp = StateFingerprinter()
    controls = _yes_no_continue("q05")
    entry = fp.fingerprint(url=URL, controls=controls)
    identity = WalkIdentity(
        fp, entry_fingerprint=entry,
        entry_signals=StepSignals(base=entry,
                                  structural=structural_signature(controls)))
    expanded, _ = identity.identify(url=URL, controls=controls,
                                    revealed=("text:describe your condition",))
    assert expanded != entry


# ─── T-SI-02 · perceptual identity ────────────────────────────────────────────

def test_perceptual_hash_participates_regardless_of_dom_richness():
    """The DOM-sparse gate is gone: the hasher honours what it is given."""
    rich = [{"role": "button", "name": "b%d" % i} for i in range(8)]
    assert (state_fingerprint(URL, rich, perceptual_hash="aaaa")
            != state_fingerprint(URL, rich, perceptual_hash="bbbb"))


def test_walk_identity_never_supplies_pixels_for_a_dom_separated_page():
    """THE INVARIANT THE OLD GATE PROTECTED, relocated. A rich-DOM page's
    cosmetic repaint must not fragment its state — now guaranteed by the layer
    that decides, not by counting controls inside the hasher."""
    fp = StateFingerprinter()
    page_a = [{"role": "button", "name": "Save"}, {"role": "link", "name": "Home"}]
    page_b = [{"role": "button", "name": "Delete"}, {"role": "link", "name": "Home"}]
    entry = fp.fingerprint(url=URL, controls=page_a)
    identity = WalkIdentity(fp, entry_fingerprint=entry)
    # The DOM already separates these, so rung 4 is never reached...
    assert not identity.needs_perception(url=URL, controls=page_b)
    # ...and the identity emitted is the HISTORICAL one, byte for byte.
    digest, _ = identity.identify(url=URL, controls=page_b, perceptual_hash="zzzz")
    assert digest == fp.fingerprint(url=URL, controls=page_b)


def test_pixels_separate_screens_no_dom_signal_can():
    """The canvas / DOM-opaque case: identical (empty) DOM, different screens."""
    fp = StateFingerprinter()
    entry = fp.fingerprint(url=URL, controls=[])
    identity = WalkIdentity(
        fp, entry_fingerprint=entry,
        entry_signals=StepSignals(base=entry, perceptual="aaaa"))
    assert identity.needs_perception(url=URL, controls=[])
    screen2, _ = identity.identify(url=URL, controls=[], perceptual_hash="bbbb")
    assert screen2 != entry


def test_an_unmeasured_perceptual_hash_is_not_a_pixel_change():
    """"" means NOT MEASURED, never "blank screen". Comparing a measured hash
    against an unmeasured one used to read as a difference, so the first advance
    of every walk looked perceptually distinct from its entry step whatever was
    on screen."""
    fp = StateFingerprinter()
    controls = _yes_no_continue("q01")
    entry = fp.fingerprint(url=URL, controls=controls)
    identity = WalkIdentity(
        fp, entry_fingerprint=entry,
        entry_signals=StepSignals(base=entry,
                                  structural=structural_signature(controls)))
    # entry was never measured; this observation is. Nothing can be concluded.
    same, _ = identity.identify(url=URL, controls=controls, perceptual_hash="ffff")
    assert same == entry


# ─── The no-op pairing: distinctness must be EARNED ───────────────────────────

def test_a_no_op_click_does_not_mint_a_state():
    """Twenty identical screens with NO discriminating signal stay one state —
    the property a step counter in the digest would destroy."""
    digests = _walk([_yes_no_continue("same") for _ in range(20)])
    assert len(set(digests)) == 1


def test_returning_to_an_earlier_step_reuses_its_identity():
    """Loop detection survives. q1 → q2 → q3 → q2 must re-emit q2's identity, or
    an application that sends the user backwards is walked until the budget
    runs out and reported as progress."""
    fp = StateFingerprinter()
    steps = [_yes_no_continue("q%02d" % i) for i in (1, 2, 3, 2)]
    digests = _walk(steps, fp)
    assert digests[1] == digests[3]
    assert len({digests[0], digests[1], digests[2]}) == 3


def test_ordinal_is_not_an_identity_input_for_the_walk():
    """The walk's identities must not move just because the counter did."""
    identical = [_yes_no_continue("same") for _ in range(5)]
    assert len(set(_walk(identical))) == 1


def test_identity_is_reproducible_across_runs():
    """DETERMINISM. Two independent walks of the same twenty screens produce the
    same twenty digests — the property the golden-manifest gate depends on."""
    steps = [_yes_no_continue("q%02d" % i) for i in range(1, 21)]
    assert _walk(steps) == _walk(steps)


def test_walk_identity_instances_share_nothing():
    """NO HIDDEN GLOBAL STATE: one instance per journey, and one journey's
    history must not reach another's."""
    steps = [_yes_no_continue("q%02d" % i) for i in range(1, 6)]
    a, b = _walk(steps), _walk(steps)
    assert a == b
    fp = StateFingerprinter()
    entry = fp.fingerprint(url=URL, controls=steps[0])
    one, two = (WalkIdentity(fp, entry_fingerprint=entry),
                WalkIdentity(fp, entry_fingerprint=entry))
    d, sig = one.identify(url=URL, controls=steps[1])
    one.advance(d, sig)
    assert one.ordinal == 1 and two.ordinal == 0
    assert two.previous_fingerprint == entry


def test_resync_records_a_re_observation_without_counting_a_step():
    """Answering a question re-observes the step we are ALREADY on. Counting it
    as an advance would inflate the depth metric T-SI-06 exists to make
    trustworthy."""
    fp = StateFingerprinter()
    controls = _yes_no_continue("q01")
    entry = fp.fingerprint(url=URL, controls=controls)
    identity = WalkIdentity(fp, entry_fingerprint=entry)
    digest, signals = identity.identify(url=URL, controls=controls,
                                        revealed=("text:detail",))
    identity.resync(digest, signals)
    assert identity.ordinal == 0
    assert identity.previous_fingerprint == digest

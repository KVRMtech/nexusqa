"""B2 — ONE RETRY IS EARNED BY EVIDENCE, NEVER TAKEN.

Two guards, tested separately because they fail independently:

* the PURE GATE (:func:`app.refusal_repair.may_repair_retry`) — the licence,
  derived from observations;
* the LEDGER REFUND (:meth:`app.boundary.CrossingLedger.refund_app_refused`)
  — the arithmetic that keeps it to one, per boundary, ever, resume included.

THE EVIDENCE STANDARD, inherited from the step-back suite: every refusal below
is paired with a falsification control that flips exactly one axis and requires
the refused thing to happen.  An absence-only assertion is satisfied equally
well by a broken mechanism and by a working guard, and cannot tell them apart.
"""
from __future__ import annotations

from app import refusal_repair
from app.boundary import CrossingLedger

_URL = "http://x/underwriting/new-business/new-application"
_TRIGGER = "commit:Submit Application"

#: Every axis in its permitting state; each test flips exactly one.
_OK = dict(
    crossing_spent=True,
    confirmation_rung="",
    url_before=_URL,
    url_after=_URL,
    named_for_trigger=1,
    mutations_allowed=0,
    repair_ready=True,
    retries_taken=0,
    max_retries=1,
)


def _verdict(**over):
    return refusal_repair.may_repair_retry(**{**_OK, **over})


# ── the gate, axis by axis ─────────────────────────────────────────────────

def test_the_fully_evidenced_shape_is_permitted():
    """THE CONTROL for every refusal below: with every observation in the
    permitting state, the gate must open — otherwise each refusal test would
    pass against a gate that refuses everything."""
    v = _verdict()
    assert v.permitted and v.reason == "named_refusal_repaired"


def test_a_zero_budget_refuses__and_one_permits():
    assert not _verdict(max_retries=0).permitted
    assert _verdict(max_retries=0).reason == "retries_disabled"
    assert _verdict(max_retries=1).permitted


def test_a_spent_retry_budget_refuses__and_a_fresh_one_permits():
    assert _verdict(retries_taken=1).reason == "retry_budget_spent"
    assert _verdict(retries_taken=0).permitted


def test_an_unspent_crossing_refuses__a_spent_one_permits():
    """A retry is a second attempt at a RECORDED crossing; a caller holding an
    unspent boundary is confused and must not click."""
    assert _verdict(crossing_spent=False).reason == "crossing_not_spent"
    assert _verdict(crossing_spent=True).permitted


def test_a_confirmed_journey_is_never_disturbed():
    assert _verdict(confirmation_rung="text_transition").reason == "confirmed"
    assert _verdict(confirmation_rung="").permitted


def test_a_commit_that_navigated_refuses__a_fragment_move_permits():
    """Same-document is fragment- and slash-insensitive, exactly as the
    step-back gate reads it — ``#step-4`` is not a landing."""
    assert _verdict(url_after="http://x/somewhere/else").reason == "navigated"
    assert _verdict(url_after=_URL + "#step-4").permitted
    assert _verdict(url_after=_URL + "/").permitted
    assert _verdict(url_after="").permitted, (
        "an empty url_after is the silent shape itself")


def test_nothing_named_means_nothing_retried():
    """Rule 1 of the fill-time repair loop, held at the commit: a retry must be
    CAUSED by an observed rejection.  No signal, no retry."""
    assert _verdict(named_for_trigger=0).reason == "nothing_named"
    assert _verdict(named_for_trigger=1).permitted


def test_one_allowed_mutation_in_the_window_refuses():
    """Invariant 4.  A POST the guard let through may have reached the
    application; whether it took effect is unknowable, so the retry could be a
    second submission.  Fail closed."""
    assert _verdict(mutations_allowed=1).reason == "mutation_observed"
    assert _verdict(mutations_allowed=0).permitted


def test_an_unrepaired_page_refuses():
    """Clicking the commit with nothing re-filled would re-submit the very
    values the application just refused."""
    assert _verdict(repair_ready=False).reason == "repair_not_ready"
    assert _verdict(repair_ready=True).permitted


# ── the operator's dial ────────────────────────────────────────────────────

def test_the_env_dial_zero_disables(monkeypatch):
    monkeypatch.setenv("QEC_REFUSAL_RETRY_MAX", "0")
    assert refusal_repair.max_retries_configured() == 0
    assert not _verdict(max_retries=None).permitted


def test_the_env_dial_defaults_to_one(monkeypatch):
    monkeypatch.delenv("QEC_REFUSAL_RETRY_MAX", raising=False)
    assert refusal_repair.max_retries_configured() == 1
    assert _verdict(max_retries=None).permitted


def test_a_malformed_dial_fails_closed_to_zero(monkeypatch):
    """A broken dial must not enable retries — the failure mode of this
    mechanism is a form-submission spree, so every default leans off."""
    monkeypatch.setenv("QEC_REFUSAL_RETRY_MAX", "banana")
    assert refusal_repair.max_retries_configured() == 0


# ── the crossing window's mutation count ───────────────────────────────────

def test_reads_are_not_mutations():
    assert refusal_repair.mutations_allowed_in(
        [{"method": "GET", "url": "http://x/api/config"},
         {"method": "get", "url": "http://x/api/quote"}]) == 0


def test_each_mutating_method_counts():
    events = [{"method": m, "url": "http://x/api/v1/apps"}
              for m in ("POST", "PUT", "PATCH", "DELETE", "post")]
    assert refusal_repair.mutations_allowed_in(events) == 5


def test_a_truncated_window_counts_as_a_mutation():
    """A window we did not see all of is a window we cannot certify as
    mutation-free — the marker itself refuses the retry."""
    assert refusal_repair.mutations_allowed_in(
        [{"event": "buffer_truncated", "method": "", "url": ""}]) == 1


def test_junk_entries_are_ignored_not_counted():
    assert refusal_repair.mutations_allowed_in(
        [None, "POST", 7, {"no_method": True}]) == 0


# ── which named records can drive a re-fill ────────────────────────────────

def _rec(field, *, on=_TRIGGER, rule="Minimum face amount is $10,000"):
    return {"field": field, "rule": rule, "rejected_on": on}


def test_only_records_for_this_commit_qualify():
    records = [_rec("Face Amount ($)"),
               _rec("ZIP Code", on="advance:Continue"),
               _rec("Email Address", on="commit:Bind policy")]
    out = refusal_repair.repairable_rejections(records, trigger=_TRIGGER)
    assert [r["field"] for r in out] == ["Face Amount ($)"]


def test_a_page_level_rule_with_no_field_drives_nothing():
    """Real evidence, no target: attributing it to a control would be the
    invention rung 5 of the attribution ladder exists to prevent."""
    records = [_rec(""), _rec("Face Amount ($)")]
    out = refusal_repair.repairable_rejections(records, trigger=_TRIGGER)
    assert [r["field"] for r in out] == ["Face Amount ($)"]


def test_an_empty_trigger_matches_nothing():
    assert refusal_repair.repairable_rejections(
        [_rec("Face Amount ($)")], trigger="") == []


# ── the ledger's refund: once, per boundary, ever ──────────────────────────

_FP = "fp-review-step"
_NAME = "Submit Application"


def _reserved_ledger():
    led = CrossingLedger()
    rec = led.reserve(control_name=_NAME, url=_URL, state_fingerprint=_FP,
                      approval_id="apr_x", sequence_index=0, now_ms=1)
    return led, rec


def test_a_refund_reopens_the_boundary_for_exactly_one_retry():
    led, rec = _reserved_ledger()
    assert led.would_exceed(control_name=_NAME, url=_URL,
                            state_fingerprint=_FP, max_crossings=1), (
        "control: the reserved boundary must read as spent before the refund")
    assert led.refund_app_refused(control_name=_NAME, url=_URL,
                                  state_fingerprint=_FP)
    assert not led.would_exceed(control_name=_NAME, url=_URL,
                                state_fingerprint=_FP, max_crossings=1), (
        "the refunded boundary must admit ONE more reservation")
    retry = led.reserve(control_name=_NAME, url=_URL, state_fingerprint=_FP,
                        approval_id="apr_x", sequence_index=1, now_ms=2)
    assert retry.crossing_id != rec.crossing_id, (
        "the retry is a NEW crossing with its own id, never a rewrite")
    assert led.would_exceed(control_name=_NAME, url=_URL,
                            state_fingerprint=_FP, max_crossings=1), (
        "after the retry the boundary is spent again")


def test_the_second_refund_is_refused_whatever_the_evidence_says():
    led, _rec_ = _reserved_ledger()
    assert led.refund_app_refused(control_name=_NAME, url=_URL,
                                  state_fingerprint=_FP)
    led.reserve(control_name=_NAME, url=_URL, state_fingerprint=_FP,
                approval_id="apr_x", sequence_index=1, now_ms=2)
    assert not led.refund_app_refused(control_name=_NAME, url=_URL,
                                      state_fingerprint=_FP), (
        "one refund per boundary, EVER — this is the second brake, and it "
        "does not ask why")
    assert not led.has_refund(control_name=_NAME, url=_URL)


def test_a_refund_with_nothing_reserved_is_refused():
    led = CrossingLedger()
    assert not led.refund_app_refused(control_name=_NAME, url=_URL,
                                      state_fingerprint=_FP)


def test_is_spent_keeps_answering_true_after_a_refund():
    """The step-back gate's invariant 1 and the resume logic both read
    ``is_spent``; the refund must never make an operated boundary look
    untouched."""
    led, _rec_ = _reserved_ledger()
    led.refund_app_refused(control_name=_NAME, url=_URL, state_fingerprint=_FP)
    assert led.is_spent(control_name=_NAME, url=_URL, state_fingerprint=_FP)


def test_a_resumed_ledger_cannot_be_reopened_by_a_fresh_refund():
    """THE KILL-BETWEEN-REFUND-AND-RETRY CASE.  Refunds live in RAM only; the
    journal restores every reservation as spent.  A resumed run that asks for
    a refund gets one — and the boundary STAYS shut, because both journalled
    reservations count against the budget.  A duplicate irreversible click is
    unreachable by construction, not by luck."""
    led, first = _reserved_ledger()
    led.refund_app_refused(control_name=_NAME, url=_URL, state_fingerprint=_FP)
    retry = led.reserve(control_name=_NAME, url=_URL, state_fingerprint=_FP,
                        approval_id="apr_x", sequence_index=1, now_ms=2)
    resumed = CrossingLedger()
    assert resumed.restore([first.to_dict(), retry.to_dict()]) == 2
    assert resumed.would_exceed(control_name=_NAME, url=_URL,
                                state_fingerprint=_FP, max_crossings=1)
    # Even taking the refund a resumed run is entitled to, the budget holds:
    # two journalled reservations minus one refund still meets max_crossings.
    assert resumed.refund_app_refused(control_name=_NAME, url=_URL,
                                      state_fingerprint=_FP)
    assert resumed.would_exceed(control_name=_NAME, url=_URL,
                                state_fingerprint=_FP, max_crossings=1), (
        "a resume must never turn the one-retry refund into a second click")


def test_the_refund_leaves_a_multi_crossing_grant_arithmetic_intact():
    """An operator's ``max_crossings=2`` still authorises exactly two
    irreversible acts: one app-refused attempt is refunded, and the count of
    ACTS the grant measures is unchanged."""
    led = CrossingLedger()
    led.reserve(control_name=_NAME, url=_URL, state_fingerprint="fp1",
                approval_id="apr_x", sequence_index=0, now_ms=1)
    assert led.refund_app_refused(control_name=_NAME, url=_URL,
                                  state_fingerprint="fp1")
    led.reserve(control_name=_NAME, url=_URL, state_fingerprint="fp1",
                approval_id="apr_x", sequence_index=1, now_ms=2)
    assert not led.would_exceed(control_name=_NAME, url=_URL,
                                state_fingerprint="fp2", max_crossings=2), (
        "one refunded attempt + one real act leaves one authorised act open")
    led.reserve(control_name=_NAME, url=_URL, state_fingerprint="fp2",
                approval_id="apr_x", sequence_index=2, now_ms=3)
    assert led.would_exceed(control_name=_NAME, url=_URL,
                            state_fingerprint="fp3", max_crossings=2)

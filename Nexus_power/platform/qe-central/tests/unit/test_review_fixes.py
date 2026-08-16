"""Fixes from the deep architecture review — the ones with teeth.

Each of these was a real divergence between what one layer enforced and what
another allowed. They share a shape worth naming: a rule existed, was correct,
and was reachable through only ONE of the doors that led to the thing it
guarded.

  * a submit-approval guard on PATCH but not on create or env profiles;
  * a vision flag enforced at dispatch but not at the endpoint that spends money;
  * an env-kind vocabulary enforced in one module and re-typed in another;
  * a status the API reported and the database never held.

A guard on one door is not a guard.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.apps import (
    _ENV_KINDS,
    _reject_advance_shadowing_approvals,
)
from app.security.prod_guard import CRAWLABLE_ENV_KINDS


# ── advance-shadowing approvals: every door, not just PATCH ─────────────────

_SHADOWING = {"allow_submit": True, "submit_approvals": ["Continue"]}


def test_a_funnel_breaking_approval_is_refused():
    """Approving "Continue" as a submit makes every wizard step using that label
    unwalkable — live, a five-step quote funnel recorded as five one-step
    journeys with no error anywhere."""
    with pytest.raises(HTTPException) as exc:
        _reject_advance_shadowing_approvals(_SHADOWING)
    assert exc.value.status_code == 422
    assert "Continue" in str(exc.value.detail)


@pytest.mark.parametrize("label", ["continue", "NEXT", " Proceed ", "go", "resume"])
def test_every_generic_advance_word_is_refused_however_cased(label):
    with pytest.raises(HTTPException):
        _reject_advance_shadowing_approvals(
            {"allow_submit": True, "submit_approvals": [label]})


def test_a_real_final_submit_label_is_allowed():
    """The guard must not block the legitimate approval it exists to steer
    operators toward."""
    for label in ("Submit Application", "Place Order", "See My Quote", "Pay Now"):
        _reject_advance_shadowing_approvals(
            {"allow_submit": True, "submit_approvals": [label]})


def test_the_guard_is_wired_into_every_write_path():
    """THE ACTUAL DEFECT. The guard was called only from the app PATCH handler,
    so the same fences could be seeded at CREATE, or on an env profile whose
    fences overlay the app's at resolve time. A guard on one door is not a
    guard — this asserts the call exists on all four.

    M0.5 T-SEC-04 folded the advance-shadowing guard and the egress-host policy
    into ONE fences write gate, ``_validated_fences``. The invariant is
    unchanged and stricter: every write path routes through the single gate, and
    the gate itself still runs the shadowing check.
    """
    import inspect

    from app.routers import apps

    for fn_name in ("create_app", "update_app",
                    "create_environment", "update_environment"):
        src = inspect.getsource(getattr(apps, fn_name))
        assert "_validated_fences" in src, (
            f"{fn_name} accepts fences without the write gate — a "
            f"funnel-breaking approval or an unsafe egress host can be written "
            f"through it")

    gate = inspect.getsource(apps._validated_fences)
    assert "_reject_advance_shadowing_approvals" in gate
    assert "validate_allowed_hosts" in gate


# ── env-kind vocabulary: one source ────────────────────────────────────────

def test_the_writable_vocabulary_cannot_drift_from_the_enforceable_one():
    assert _ENV_KINDS == frozenset(CRAWLABLE_ENV_KINDS)


# ── vision: the flag is enforced where the money is spent ──────────────────

def test_both_vision_endpoints_enforce_the_server_flag():
    """The signature proves WHO is asking, never WHAT they may spend.
    ``vision-operate`` trusted the dispatch-time decision — a flag its CALLER
    evaluated — so an explorer from a stale image, or a crawl dispatched before
    the flag was turned off, kept billing vision calls the operator had
    disabled. ``perceive-controls`` already gated here and its docstring named
    this very gap."""
    import inspect

    from app.routers import internal

    for fn_name in ("vision_operate", "perceive_controls_endpoint"):
        src = inspect.getsource(getattr(internal, fn_name))
        assert "crawl_vision_enabled" in src, (
            f"{fn_name} does not enforce the server-side vision flag")


# ── the exploration status the API reports is the one it stores ────────────

def test_dispatch_transition_is_compare_and_set():
    """'dispatched' is now persisted, so the row matches the response. It must
    be a COMPARE-AND-SET: a fast crawl can call back /complete before the
    dispatch handler resumes, and an unconditional write would regress a
    finished crawl to an earlier state — inventing a status it had left."""
    import inspect

    from app.routers import explorations

    src = inspect.getsource(explorations._mark_if_status)
    assert "== expect" in src, "the dispatch status write is not conditional"

    dispatch_src = inspect.getsource(explorations._dispatch_explorer)
    assert '_mark_if_status(' in dispatch_src
    assert 'expect="pending"' in dispatch_src


def test_the_model_docstring_enumerates_every_status_written():
    """Four ACTIVE-status sets key off this vocabulary; a value missing from the
    docstring is how one of them silently drifts."""
    from app.db.models import QEExplorationRow

    doc = QEExplorationRow.__doc__ or ""
    for status in ("pending", "dispatched", "writing", "running",
                   "completed", "failed", "refused", "stalled"):
        assert status in doc, f"{status!r} is written but undocumented"


# ── soft-delete removes the login map, not just the ciphertext ─────────────

def test_soft_delete_clears_the_login_recipe_too():
    """The recording holds no credential VALUES, which is why it was left
    behind — but it is still a map of how to sign in to the client's system,
    and a deleted app has no business retaining one."""
    import inspect

    from app.routers import apps

    src = inspect.getsource(apps.delete_app)
    assert "row.creds_blob = None" in src
    assert "row.login_recording = {}" in src, (
        "soft-delete zeroes the ciphertext but retains the login shape")

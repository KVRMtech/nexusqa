"""E2E REFUSES RATHER THAN DEGRADING SILENTLY.

Traversal posture is derived from the environment attestation, so an expired or
downgraded one resolves full → probe. Everything downstream then quietly follows:
agent data-fill switches off, the wizard budget drops to a six-step probe, the
deeper advance tiers never run — and the crawl still reports "completed".

An operator who explicitly selected END-TO-END gets a shallow walk and a green
result, with nothing anywhere saying the two differ. That is the exact
silent-degradation failure this product exists to refuse, committed by the
product itself.

A ten-second refusal naming the cause is worth more than a forty-five-minute
crawl that answers a question nobody asked: the operator can re-attest and re-run,
but they cannot un-believe a green "completed".
"""
from __future__ import annotations

import inspect
from datetime import timedelta

from app.db import utc_now
from app.routers import explorations
from app.routers.explorations import _posture_shortfall_cause
from app.security import prod_guard


# ── the refusal exists and is wired ────────────────────────────────────────

def test_dispatch_refuses_an_e2e_request_it_cannot_honour():
    src = inspect.getsource(explorations._dispatch_explorer)
    assert 'crawl_mode == "e2e"' in src
    assert "prod_guard.TRAVERSAL_FULL" in src
    assert "e2e_posture_unavailable" in src, (
        "e2e silently degrades to a probe walk instead of refusing")


def test_the_refusal_is_scoped_to_an_EXPLICIT_e2e_request():
    """A planned branch walk forces e2e by definition and must not be refused
    for it; an app that never asked for e2e is untouched. The guard exists to
    protect an operator's stated intent, not to block the system's own."""
    src = inspect.getsource(explorations._dispatch_explorer)
    assert "not walk_plan" in src


def test_the_refusal_reports_both_requested_and_actual():
    """A refusal a client cannot act on is just a different silence."""
    src = inspect.getsource(explorations._dispatch_explorer)
    assert '"requested"' in src and '"actual"' in src


# ── the cause is specific enough to fix ────────────────────────────────────

def _att(**over):
    att = {
        "env_kind": "disposable",
        "attested_by": "sre@client.example",
        "expires_at": (utc_now() + timedelta(hours=24)).isoformat(),
    }
    att.update(over)
    return att


def test_an_expired_attestation_names_the_date():
    """'attestation expired 2026-07-12' is fixable in thirty seconds;
    'traversal is probe' sends an operator into the code."""
    expired = (utc_now() - timedelta(days=33)).isoformat()
    cause = _posture_shortfall_cause(_att(expires_at=expired),
                                     prod_guard.TRAVERSAL_PROBE)
    assert "EXPIRED" in cause
    assert (utc_now() - timedelta(days=33)).date().isoformat() in cause


def test_a_missing_attestation_says_so():
    cause = _posture_shortfall_cause({}, prod_guard.TRAVERSAL_PROBE)
    assert "no environment attestation" in cause


def test_an_unattributed_attestation_says_nobody_signed_it():
    cause = _posture_shortfall_cause(_att(attested_by=""),
                                     prod_guard.TRAVERSAL_PROBE)
    assert "no attester" in cause


def test_production_is_named_as_deliberate_not_as_a_fault():
    """Observe posture on prod is correct behaviour, not a misconfiguration —
    the message must not send an operator hunting for a broken attestation."""
    cause = _posture_shortfall_cause(_att(env_kind="prod"),
                                     prod_guard.TRAVERSAL_OBSERVE)
    assert "production" in cause and "read-only" in cause


def test_an_unparseable_expiry_is_reported_verbatim():
    cause = _posture_shortfall_cause(_att(expires_at="whenever"),
                                     prod_guard.TRAVERSAL_PROBE)
    assert "no readable expiry" in cause


def test_an_unknown_kind_names_the_kind():
    cause = _posture_shortfall_cause(_att(env_kind="qa-ish"),
                                     prod_guard.TRAVERSAL_PROBE)
    assert "qa-ish" in cause


def test_every_cause_is_a_sentence_a_person_can_act_on():
    for att, posture in (
        ({}, prod_guard.TRAVERSAL_PROBE),
        (_att(attested_by=""), prod_guard.TRAVERSAL_PROBE),
        (_att(expires_at="nope"), prod_guard.TRAVERSAL_PROBE),
        (_att(env_kind="prod"), prod_guard.TRAVERSAL_OBSERVE),
        (_att(env_kind="weird"), prod_guard.TRAVERSAL_PROBE),
    ):
        cause = _posture_shortfall_cause(att, posture)
        assert cause and cause[0].isupper() and cause.rstrip().endswith(".")

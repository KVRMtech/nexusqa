"""TRAVERSAL POSTURE — how far a crawl may walk a business journey.

The product's scope is TEST-environment autonomous discovery: point it at an
attested non-production environment and it should walk each business journey from
start to finish and catalogue it. What it did instead was SAMPLE — six wizard
steps of a fifteen-step application funnel — and report the sample as a journey.

The cause was that traversal depth had no owner. It was a side-effect of
``crawl_mode``, a SCOPE dial the operator sets to say WHICH pages to visit, so an
app onboarded with the default scope silently got a probe-sized walk of every
funnel it found. Live on a carrier admin app: six flows, every one of them
recorded at ``steps: 1``.

``prod_guard.traversal_posture`` gives depth its own owner, derived from the
attestation the operator has ALREADY signed — so a test environment needs no
second dial set by hand, and an environment nobody has attested is never driven
deeply on a guess.

WHAT THIS IS NOT: a safety dial. Nothing here decides what may be CLICKED. The
refuse-pack danger gate, the commit boundary and the disposable-only submit tier
(:func:`prod_guard.submit_approvals`) are untouched by the posture and are
re-verified at click time inside the explorer. These tests pin that separation
directly — ``full`` traversal on a staging env must NOT confer submit.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.db import utc_now
from app.security.prod_guard import (
    ENV_KIND_DISPOSABLE,
    ENV_KIND_PROD,
    ENV_KIND_PRODUCTION_TEST,
    ENV_KIND_STAGING,
    ENV_KIND_UAT,
    NON_PROD_ENV_KINDS,
    TRAVERSAL_FULL,
    TRAVERSAL_OBSERVE,
    TRAVERSAL_POSTURES,
    TRAVERSAL_PROBE,
    submit_approvals,
    traversal_posture,
)


def _iso(hours: int) -> str:
    return (utc_now() + timedelta(hours=hours)).isoformat()


def _app(att: dict | None = None, fences: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        app_id="app-1", env_attestation=dict(att or {}),
        fences=dict(fences or {}), schedule={}, status="active",
    )


def _attested(env_kind: str, *, expires_hours: int = 24,
              attested_by: str = "sre@client.example") -> dict:
    return {
        "env_kind": env_kind,
        "attested_by": attested_by,
        "expires_at": _iso(expires_hours),
        "reset_procedure": "terraform destroy && apply",
    }


# ── the three postures ──────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", sorted(NON_PROD_ENV_KINDS))
def test_every_attested_nonprod_kind_walks_journeys_to_the_end(kind):
    """A signed statement that this is a test environment IS the authorisation to
    walk its journeys fully. Requiring a second, separate dial on top of it is the
    friction that produced one-step journeys on an app that was correctly
    onboarded."""
    assert traversal_posture(_app(_attested(kind))) == TRAVERSAL_FULL


def test_production_is_catalogue_only():
    assert traversal_posture(_app(_attested(ENV_KIND_PROD))) == TRAVERSAL_OBSERVE


def test_production_stays_observe_even_with_a_broken_attestation():
    """The prod check is on env_kind alone and runs FIRST, so an expired or
    unattributed prod attestation can never fall through to a deeper posture —
    a malformed row must not be a way to start driving production."""
    for att in (
        _attested(ENV_KIND_PROD, expires_hours=-1),
        _attested(ENV_KIND_PROD, attested_by=""),
        {"env_kind": ENV_KIND_PROD},
    ):
        assert traversal_posture(_app(att)) == TRAVERSAL_OBSERVE


# ── fail-closed: no signed statement ⇒ no deep drive ────────────────────────

def test_an_app_with_no_attestation_is_only_probed():
    assert traversal_posture(_app()) == TRAVERSAL_PROBE


def test_an_expired_attestation_drops_back_to_probe():
    """Attestations expire on purpose — 'this was a throwaway env last quarter'
    is not a statement about today."""
    assert traversal_posture(
        _app(_attested(ENV_KIND_DISPOSABLE, expires_hours=-1))) == TRAVERSAL_PROBE


def test_an_unattributed_attestation_drops_back_to_probe():
    """No named attester means nobody has actually taken responsibility for the
    claim, so it is not a claim."""
    assert traversal_posture(
        _app(_attested(ENV_KIND_DISPOSABLE, attested_by=""))) == TRAVERSAL_PROBE


@pytest.mark.parametrize("kind", ["", "production", "PRD", "throwaway", "qa"])
def test_an_unrecognised_env_kind_drops_back_to_probe(kind):
    """A label the vocabulary does not know is not evidence of anything. Note
    'production' and 'PRD' land on PROBE rather than OBSERVE — they are not the
    canonical prod kind, so they get the fail-closed posture, and the separate
    ``resolve_effective_fences`` gate still forces allow_submit off for them."""
    assert traversal_posture(_app(_attested(kind))) == TRAVERSAL_PROBE


def test_posture_is_always_one_of_the_declared_three():
    """Downstream (the explorer) fails closed on an unknown posture string; this
    pins that the producer can only ever emit a value the consumer knows."""
    for att in (None, {}, _attested("disposable"), _attested("prod"),
                _attested("nonsense"), {"env_kind": None}):
        assert traversal_posture(_app(att)) in TRAVERSAL_POSTURES


# ── THE SEPARATION: traversal is not permission ─────────────────────────────

def test_full_traversal_on_staging_confers_no_submit_rights():
    """The load-bearing invariant of this whole change.

    Staging/UAT/production_test all walk journeys fully — that is a DEPTH
    decision. Crossing a real submit MUTATES the environment, and that stays
    disposable-attested only. If these two ever collapse into one dial, a deeper
    crawl becomes a laxer one, which is the thing the crawler must never be.
    """
    for kind in (ENV_KIND_STAGING, ENV_KIND_UAT, ENV_KIND_PRODUCTION_TEST):
        app = _app(_attested(kind), fences={"allow_submit": True,
                                            "submit_approvals": ["Continue"]})
        assert traversal_posture(app) == TRAVERSAL_FULL, kind
        assert submit_approvals(app) == [], (
            f"{kind} must not confer submit rights — only a disposable env does")


def test_disposable_gets_both_full_traversal_and_the_submit_blanket():
    app = _app(_attested(ENV_KIND_DISPOSABLE))
    assert traversal_posture(app) == TRAVERSAL_FULL
    assert "*" in submit_approvals(app)


def test_probe_posture_app_also_has_no_submit():
    """Fail-closed on both axes at once for an unattested app."""
    app = _app(fences={"allow_submit": True, "submit_approvals": ["Pay"]})
    assert traversal_posture(app) == TRAVERSAL_PROBE
    assert submit_approvals(app) == []


# ── purity (the guard's stated contract) ────────────────────────────────────

def test_traversal_posture_reads_a_mapping_row_too():
    """prod_guard's contract is 'ORM row OR mapping OR namespace'; the dispatcher
    passes an ORM row, tests pass namespaces, and the compliance adapter passes
    dicts."""
    assert traversal_posture(
        {"env_attestation": _attested(ENV_KIND_DISPOSABLE)}) == TRAVERSAL_FULL
    assert traversal_posture({"env_attestation": {}}) == TRAVERSAL_PROBE


def test_traversal_posture_never_mutates_the_row():
    att = _attested(ENV_KIND_DISPOSABLE)
    app = _app(att)
    before = dict(app.env_attestation)
    traversal_posture(app)
    assert app.env_attestation == before

"""A4.3 / T-AC-02 — the per-control approval seam, qe-central side.

``prod_guard.boundary_approvals`` is the gate between a tenant's stored config
and the crawl process.  It is deliberately STRICTER than its sibling
``submit_approvals``: a disposable attestation mints a ``"*"`` blanket there and
mints nothing here.  The blanket exists to spare an operator the ceremony of
naming safe submits on a throwaway environment; for an irreversible one the
ceremony is the entire point.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.security import prod_guard
from app.security.prod_guard import _utcnow as utc_now


# ── fixtures, matching the shapes prod_guard actually reads ─────────────────

def _future_iso(hours: int = 24) -> str:
    return (utc_now() + timedelta(hours=hours)).isoformat()


def _past_iso(hours: int = 1) -> str:
    return (utc_now() - timedelta(hours=hours)).isoformat()


def _attestation(env_kind: str = "staging", *, expires: str | None = None) -> dict:
    return {
        "env_kind": env_kind,
        "attested_by": "sre@client.example",
        "attested_at": _past_iso(2),
        "expires_at": expires if expires is not None else _future_iso(24),
        "reset_procedure": "terraform destroy && apply",
        "authorization": {"authorized": True, "authorized_by": "ciso@client.example"},
        "rules_of_engagement": {"signed": True, "signed_by": "ciso@client.example",
                                "version": "roe-v1"},
        "preflight": {"passed": True, "at": _past_iso(1)},
    }


def _app(*, env_attestation: dict | None = None, fences: dict | None = None):
    return SimpleNamespace(
        app_id="app-1",
        env_attestation=dict(env_attestation if env_attestation is not None
                             else _attestation()),
        fences=dict(fences or {}),
        schedule={},
        status="active",
    )


GRANT = {"control": "Submit Application",
         "url": "https://app.example/underwriting/new-application",
         "approved_by": "operator@nexus.test"}


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_stored_grant_reaches_the_crawl():
    out = prod_guard.boundary_approvals(_app(fences={"boundary_approvals": [GRANT]}))
    assert len(out) == 1
    assert out[0]["control"] == "Submit Application"
    assert out[0]["url"] == GRANT["url"]
    assert out[0]["approved_by"] == "operator@nexus.test"
    assert out[0]["max_crossings"] == 1


def test_a_grant_may_live_on_the_attestation_too():
    att = _attestation()
    att["boundary_approvals"] = [GRANT]
    assert len(prod_guard.boundary_approvals(_app(env_attestation=att))) == 1


def test_a_bare_label_is_accepted_and_normalised():
    out = prod_guard.boundary_approvals(
        _app(fences={"boundary_approvals": ["Submit Application"]}))
    assert out == [{"control": "Submit Application", "max_crossings": 1}]


# ── the refusals ─────────────────────────────────────────────────────────────

def test_no_grants_configured_yields_nothing():
    assert prod_guard.boundary_approvals(_app()) == []


def test_a_wildcard_is_dropped_here_as_well_as_in_the_explorer():
    """Two independent refusals. By the time a wildcard reached this function it
    would already have been PERSISTED as an operator decision, so refusing it
    only inside the crawl process would leave the bad config on record."""
    out = prod_guard.boundary_approvals(_app(fences={"boundary_approvals": [
        {"control": "*"}, {"control": "Submit*"}, GRANT]}))
    assert [g["control"] for g in out] == ["Submit Application"]


def test_an_unsigned_rules_of_engagement_grants_nothing():
    att = _attestation()
    att.pop("rules_of_engagement")
    assert prod_guard.boundary_approvals(
        _app(env_attestation=att, fences={"boundary_approvals": [GRANT]})) == []


def test_an_app_not_authorized_to_test_grants_nothing():
    att = _attestation()
    att["authorization"] = {"authorized": False}
    assert prod_guard.boundary_approvals(
        _app(env_attestation=att, fences={"boundary_approvals": [GRANT]})) == []


def test_an_expired_attestation_grants_nothing():
    att = _attestation(expires=_past_iso(1))
    assert prod_guard.boundary_approvals(
        _app(env_attestation=att, fences={"boundary_approvals": [GRANT]})) == []


def test_a_production_environment_grants_nothing():
    att = _attestation("production")
    assert prod_guard.boundary_approvals(
        _app(env_attestation=att, fences={"boundary_approvals": [GRANT]})) == []


def test_a_disposable_attestation_does_NOT_mint_a_blanket_here():
    """The load-bearing difference from ``submit_approvals``.

    That function returns ``["*", …]`` on a disposable env — every submit the
    application offers, authorised by one signed attestation. A grant is the
    least-privilege seam, so a blanket must never be able to appear in it: an
    empty stored list means an empty grant list, disposable or not.
    """
    app = _app(env_attestation=_attestation("disposable"),
               fences={"allow_submit": True})
    assert prod_guard.submit_approvals(app)[:1] == ["*"], (
        "the shipped disposable blanket must be unchanged by A4.3")
    assert prod_guard.boundary_approvals(app) == []


def test_a_malformed_max_crossings_falls_back_to_one():
    out = prod_guard.boundary_approvals(_app(fences={"boundary_approvals": [
        {"control": "Submit Application", "max_crossings": "lots"}]}))
    assert out[0]["max_crossings"] == 1


def test_a_negative_max_crossings_is_floored_at_one():
    out = prod_guard.boundary_approvals(_app(fences={"boundary_approvals": [
        {"control": "Submit Application", "max_crossings": -5}]}))
    assert out[0]["max_crossings"] == 1


# ── the seam is actually wired end to end ────────────────────────────────────

def test_the_dispatch_carries_the_grants_to_the_explorer():
    """A grant that stops at qe-central is a grant that does nothing. Asserted
    on the source because the dispatch itself needs a live DB session."""
    import inspect
    from app.routers import explorations
    src = inspect.getsource(explorations)
    assert "boundary_approvals = prod_guard.boundary_approvals(row)" in src
    assert "boundary_approvals=boundary_approvals," in src


def test_the_explorer_request_model_accepts_the_grants():
    from app.clients.explorer_client import ExploreDispatchRequest
    req = ExploreDispatchRequest(
        tenant_id="t1", exploration_id="e1", app_id="app-1", crawl_id="c1",
        target_url="https://app.example/apply", boundary_approvals=[GRANT])
    assert req.boundary_approvals == [GRANT]


def test_the_approval_picker_reads_the_new_coverage_list():
    """The operator picks a grant from ``coverage.approvable_boundary``. Before
    A4.3 the picker read ``submit_candidates``, whose two producers both filtered
    dangerous controls out — so it was built from a list that structurally could
    not contain anything needing approval."""
    import inspect
    from app.routers import apps
    src = inspect.getsource(apps)
    assert '_cov.get("approvable_boundary")' in src
    assert '_cov.get("submit_candidates")' in src, (
        "the fallback for pre-A4.3 manifests must stay, or approvals an "
        "operator already relies on are silently un-offered")

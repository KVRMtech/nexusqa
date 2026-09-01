"""R6 — PRODUCTION MUTATION (a crawl that types into a real application).

ATTACK
======
Register an app attested as ``env_kind: prod`` (or with no attestation at all)
and start a crawl.  The dispatch read ``observe_only`` from
``fences.get("observe_only")`` and nothing else — so an app whose fences simply
lacked the key was dispatched free to fill forms, click commit controls and
advance a funnel against production.

``security.prod_guard.resolve_effective_fences`` DOES force observe-only for a
production environment, but only on the multi-env ``env_resolver`` path, which a
single-env crawl never travels.  The invariant was enforced by a helper the
actual crawl path did not call.

EXPECTED
========
Mutation blocked, decided inside the crawl execution path, and re-decided
independently by the explorer from the attestation it receives — so manipulating
or bypassing configuration resolution does not lower it.
"""
from __future__ import annotations

import inspect

import pytest

from app.routers.explorations import resolve_crawl_observe_only
from app.security import prod_guard


def _att(kind: str) -> dict:
    return {"env_kind": kind, "attested_by": "qa@client.example"}


# ── the decision itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", [
    "prod", "production", "staging", "uat", "production_test",
    "", "   ", "PROD", "disposible", "unknown-label", None,
])
def test_r6_every_non_disposable_environment_is_observation_only(kind):
    att = _att(kind) if kind is not None else {}
    observe_only, _ = resolve_crawl_observe_only(att, {})
    assert observe_only is True, f"env_kind={kind!r} was dispatched mutable"


def test_r6_an_app_with_no_attestation_at_all_is_observation_only():
    """The default must be refusal, not permission."""
    assert resolve_crawl_observe_only(None, None)[0] is True
    assert resolve_crawl_observe_only({}, {})[0] is True


def test_r6_an_explicit_observe_only_fence_is_a_FLOOR_never_lowered():
    """Even a disposable environment stays observation-only if asked to be."""
    assert resolve_crawl_observe_only(_att("disposable"), {"observe_only": True})[0] is True


def test_r6_a_disposable_environment_may_still_mutate():
    """POSITIVE half — the product must keep working where it is authorised to."""
    observe_only, kind = resolve_crawl_observe_only(_att("disposable"), {})
    assert observe_only is False and kind == "disposable"


def test_r6_the_mutable_kind_matches_the_submit_doctrine():
    """The fill gate and the submit gate must name the SAME environment class.

    If they drifted, a crawl could be allowed to type somewhere it is not
    allowed to submit — which is the shape of the original bug."""
    assert prod_guard.ENV_KIND_DISPOSABLE == "disposable"
    observe_only, _ = resolve_crawl_observe_only(
        _att(prod_guard.ENV_KIND_DISPOSABLE), {})
    assert observe_only is False
    for kind in prod_guard.NON_PROD_ENV_KINDS - {prod_guard.ENV_KIND_DISPOSABLE}:
        assert resolve_crawl_observe_only(_att(kind), {})[0] is True


# ── it is enforced in the CRAWL PATH, not borrowed from a config resolver ──

def test_r6_the_dispatch_calls_the_crawl_paths_own_resolver():
    """T-SEC-05 structurally: the dispatch must not read the raw fence again."""
    from app.routers import explorations

    src = inspect.getsource(explorations._dispatch_explorer)
    assert "resolve_crawl_observe_only" in src
    assert 'observe_only=bool(fences.get("observe_only"))' not in src
    assert "observe_only=observe_only" in src
    # …and it ships the env_kind so the explorer can reach its own verdict.
    assert "env_kind=crawl_env_kind" in src


def test_r6_the_posture_is_recorded_on_the_row_as_evidence():
    """A client reading 'completed' must be able to see the crawl catalogued
    rather than walked, and why."""
    from app.routers import explorations

    src = inspect.getsource(explorations._dispatch_explorer)
    assert '"observe_only": observe_only' in src
    assert '"env_kind": crawl_env_kind' in src


def test_r6_the_dispatch_contract_carries_env_kind():
    from app.clients.explorer_client import ExploreDispatchRequest

    req = ExploreDispatchRequest(
        crawl_id="c" * 32, tenant_id="t", exploration_id="e",
        target_url="https://acme.example")
    assert req.env_kind == ""          # fail-closed default
    assert req.observe_only is False   # a floor, raised by the explorer


# ── the explorer re-decides independently (defence in depth) ───────────────

def test_r6_the_explorer_module_enforces_it_too():
    """The acceptance criterion: the block holds even if configuration
    resolution is bypassed or manipulated.

    Read as SOURCE rather than imported, because qe-explorer and qe-central both
    publish a package called ``app`` and cannot be imported into one process."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[4]
           / "engines/qe-explorer/app/main.py").read_text(encoding="utf-8")
    assert "def resolve_observe_only(" in src
    assert 'MUTABLE_ENV_KIND = "disposable"' in src
    # the crawler is built from the RESOLVED value, never the request's
    assert "observe_only=observe_only," in src
    assert "observe_only=req.observe_only," not in src
    # a dispatch that disagrees with the signed attestation loses
    assert "env_kind_mismatch" in src

"""TRAVERSAL POSTURE (explorer side) — journey depth has its own owner.

Depth used to be a side-effect of ``crawl_mode``, which is a SCOPE dial: the
operator sets it to say WHICH pages to visit. An app onboarded with the default
scope therefore got a PROBE-sized walk of every funnel it found, and a fifteen-step
application funnel was recorded as a sample and reported as a journey. Live on a
carrier admin app: six flows, every one at ``steps: 1``.

``traversal`` is now that owner, set by qe-central from the environment attestation
the operator already signed. These tests pin the three things that must hold:

  1. ``full`` walks to journey completion, INDEPENDENTLY of crawl_mode;
  2. the default is ``probe`` and is byte-identical to the previous behaviour;
  3. an unknown posture FAILS CLOSED to probe rather than to the deeper walk.

Depth is not permission: nothing here touches the refuse pack, the danger gate or
the submit tier, and no test in this file grants a click that was previously
refused.
"""
from __future__ import annotations

from app.config import Settings
from app.crawler import (
    _E2E_WIZARD_ADVANCES,
    _E2E_WIZARD_STEPS,
    _MAX_WIZARD_ADVANCES,
    _MAX_WIZARD_STEPS,
    TRAVERSAL_FULL,
    TRAVERSAL_OBSERVE,
    TRAVERSAL_PROBE,
    Budget,
    Crawler,
    GuardContext,
)
from app.guard import load_refuse_pack

_REFUSE = load_refuse_pack(Settings().refuse_pack_path)


def _crawler(tmp_path, **over) -> Crawler:
    """A Crawler built far enough to read its resolved budgets. Never run()."""
    kwargs = dict(
        crawl_id="c1", tenant_id="t1", target_url="https://app.example/",
        work_dir=str(tmp_path), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=4),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE),
    )
    kwargs.update(over)
    return Crawler(None, **kwargs)


# ── 1. full traversal walks journeys to completion ──────────────────────────

def test_full_posture_walks_to_journey_completion_even_in_explore_scope(tmp_path):
    """THE DEFECT THIS FIXES.

    ``crawl_mode='explore'`` means "visit the whole app" — it has never meant
    "only sample each funnel". An attested test environment must get the
    completion budget whatever the scope dial says, or the catalogue holds
    fragments of journeys and the generator has nothing coherent to build from.
    """
    c = _crawler(tmp_path, traversal=TRAVERSAL_FULL, crawl_mode="explore",
                 e2e_wizard_steps=60, e2e_wizard_advances=300)
    assert c._max_wizard_steps == 60
    assert c._max_wizard_advances == 300


def test_full_posture_holds_for_target_scope_too(tmp_path):
    c = _crawler(tmp_path, traversal=TRAVERSAL_FULL, crawl_mode="target",
                 e2e_wizard_steps=60, e2e_wizard_advances=300)
    assert c._max_wizard_steps == 60


# ── 2. the default is unchanged behaviour ───────────────────────────────────

def test_default_posture_is_probe_and_keeps_the_old_bounds(tmp_path):
    """Byte-identical to before the posture existed: an app nobody has attested
    is sampled, not driven."""
    c = _crawler(tmp_path)
    assert c._traversal == TRAVERSAL_PROBE
    assert c._max_wizard_steps == _MAX_WIZARD_STEPS
    assert c._max_wizard_advances == _MAX_WIZARD_ADVANCES


def test_explicit_e2e_still_gets_the_deep_walk_without_a_posture(tmp_path):
    """REGRESSION GUARD: ``crawl_mode='e2e'`` earned the deep walk before the
    posture existed and must keep it, so an app already configured that way is
    not quietly demoted by this change."""
    c = _crawler(tmp_path, crawl_mode="e2e",
                 e2e_wizard_steps=_E2E_WIZARD_STEPS,
                 e2e_wizard_advances=_E2E_WIZARD_ADVANCES)
    assert c._full_traversal is True
    assert c._max_wizard_steps == _E2E_WIZARD_STEPS
    assert c._max_wizard_advances == _E2E_WIZARD_ADVANCES


# ── 3. fail closed ──────────────────────────────────────────────────────────

def test_an_unknown_posture_falls_back_to_probe_not_to_full(tmp_path):
    """A typo, a version skew between services, or a hand-made request must
    lose depth rather than gain it."""
    for bad in ("deep", "FULL_TRAVERSAL", "1", "", None, "  "):
        c = _crawler(tmp_path, traversal=bad)
        assert c._traversal == TRAVERSAL_PROBE, f"{bad!r} must fail closed"
        assert c._max_wizard_steps == _MAX_WIZARD_STEPS


def test_posture_is_case_and_whitespace_tolerant_for_the_known_values(tmp_path):
    assert _crawler(tmp_path, traversal="  FULL ")._traversal == TRAVERSAL_FULL


def test_observe_posture_does_not_grant_the_deep_walk(tmp_path):
    """Production is catalogue-only. ``observe`` is not a non-prod posture and
    must never be routed onto the completion budget."""
    c = _crawler(tmp_path, traversal=TRAVERSAL_OBSERVE, crawl_mode="explore")
    assert c._full_traversal is False
    assert c._max_wizard_steps == _MAX_WIZARD_STEPS


# ── 4. the posture is reported, so a sample is never read as coverage ───────

def test_the_posture_is_recorded_in_coverage(tmp_path):
    """'6 steps, terminal=budget' means a walked funnel under `probe` and a
    truncated one under `full`. A reader who cannot tell them apart will read a
    sample as coverage, so the posture travels with the evidence."""
    c = _crawler(tmp_path, traversal=TRAVERSAL_FULL)
    assert c._build_coverage()["traversal"] == TRAVERSAL_FULL
    assert _crawler(tmp_path)._build_coverage()["traversal"] == TRAVERSAL_PROBE

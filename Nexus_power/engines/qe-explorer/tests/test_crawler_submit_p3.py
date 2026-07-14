"""P3 — Phase-B attested submit wired into the crawl loop. Default-OFF; fires only
when the operator supplied a per-flow approval list AND a disposable-env attestation,
and even then only for an approved, non-danger candidate (execute_submit_phase_b's
gate re-verifies). A confirmed navigation pushes the post-submit page onto the frontier.
"""
from __future__ import annotations

import asyncio
import types

from app import crawler as crawler_mod
from app.config import Settings
from app.crawler import Budget, Crawler, FrontierItem, GuardContext, Phase
from app.forms import FlowCandidate, FormFillResult, SubmitResult
from app.guard import load_refuse_pack

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)


class _DummyPort:
    """The constructor only stores the port; _maybe_submit_phase_b hands it to a
    monkeypatched execute_submit_phase_b, so no browser behaviour is needed here."""


def _build(tmp_path, *, approvals=(), attestation=None):
    guard = GuardContext(refuse_pack=_REFUSE_PACK, attestation=attestation)
    return Crawler(
        _DummyPort(),
        crawl_id="c1", tenant_id="t1", target_url="https://app.example/form",
        work_dir=str(tmp_path), refuse_pack=_REFUSE_PACK, budget=Budget(rate_per_s=0),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="fp",
        guard_context=guard, submit_approvals=approvals,
    )


def _fill(name, *, danger=False, control=None):
    fr = FormFillResult()
    fr.flow_candidates = [FlowCandidate(
        name=name, target_kind="button", danger=danger, danger_rule_id="",
        danger_severity="", control=control if control is not None else {"name": name, "kind": "button"},
    )]
    return fr


# ── the enable gate (default-OFF; needs BOTH approvals and an attestation) ───────

def test_submit_disabled_by_default(tmp_path):
    assert _build(tmp_path)._submit_enabled is False


def test_submit_requires_both_approvals_and_attestation(tmp_path):
    assert _build(tmp_path, approvals=["Continue"])._submit_enabled is False
    assert _build(tmp_path, attestation={"env_kind": "disposable"})._submit_enabled is False
    assert _build(tmp_path, approvals=["Continue"],
                  attestation={"env_kind": "disposable"})._submit_enabled is True


# ── _maybe_submit_phase_b behaviour ─────────────────────────────────────────────

def test_approved_flow_is_driven_and_post_submit_page_enqueued(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})
    seen = {}

    async def fake_submit(port, control, url, emitter, clock, **kw):
        seen["control"] = control
        seen["approved"] = kw.get("submit_flow_approved")
        seen["attestation"] = kw.get("attestation")
        seen["phase_during"] = c._guard.phase           # must be SUBMIT mid-call
        ps = types.SimpleNamespace(location="https://app.example/step2")
        return SubmitResult(submitted=True, decision=None, confirmed=True,
                            outcome="navigation", page_state=ps)

    monkeypatch.setattr(crawler_mod, "execute_submit_phase_b", fake_submit)
    item = FrontierItem(url="https://app.example/form", depth=0)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Continue", "kind": "button"}],
                                        _fill("Continue"), "fp1"))

    assert seen["approved"] is True
    assert seen["attestation"] == {"env_kind": "disposable"}
    assert seen["phase_during"] == Phase.SUBMIT
    assert c._guard.phase == Phase.EXPLORE          # restored fail-closed
    assert c._guard.submit_flow_approved is False   # restored
    assert c._forms_submitted == 1
    popped = c._frontier.pop()
    assert popped is not None and popped.url == "https://app.example/step2"
    assert popped.discovered_via == "submit:Continue"


def test_unapproved_flow_is_not_submitted(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})
    calls = {"n": 0}

    async def fake_submit(*a, **k):
        calls["n"] += 1
        return SubmitResult(submitted=True, decision=None)

    monkeypatch.setattr(crawler_mod, "execute_submit_phase_b", fake_submit)
    item = FrontierItem(url="u", depth=0)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Delete", "kind": "button"}],
                                        _fill("Delete"), "fp"))
    assert calls["n"] == 0
    assert c._forms_submitted == 0


def test_danger_candidate_is_never_submitted(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})
    calls = {"n": 0}

    async def fake_submit(*a, **k):
        calls["n"] += 1
        return SubmitResult(submitted=True, decision=None)

    monkeypatch.setattr(crawler_mod, "execute_submit_phase_b", fake_submit)
    item = FrontierItem(url="u", depth=0)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Continue", "kind": "button"}],
                                        _fill("Continue", danger=True), "fp"))
    assert calls["n"] == 0
    assert c._forms_submitted == 0


def test_unconfirmed_submit_adds_no_frontier(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})

    async def fake_submit(*a, **k):
        # submit ran (counts) but no positive terminal outcome → no deeper crawl.
        return SubmitResult(submitted=True, decision=None, confirmed=False, outcome="none")

    monkeypatch.setattr(crawler_mod, "execute_submit_phase_b", fake_submit)
    item = FrontierItem(url="u", depth=0)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Continue", "kind": "button"}],
                                        _fill("Continue"), "fp"))
    assert c._forms_submitted == 1
    assert c._frontier.pop() is None   # nothing enqueued


# ── hardening from adversarial review (#6 submit window, #7 max_depth) ───────────

def test_submit_window_bounds_the_mutating_post_burst():
    # A valid disposable-env attestation so the fine gate (classify_request) would
    # otherwise ALLOW each approved mutating POST — isolating the window bound.
    attestation = types.SimpleNamespace(is_submit_capable=lambda now_ms=None: True)
    guard = GuardContext(refuse_pack=_REFUSE_PACK, attestation=attestation)
    guard.phase = Phase.SUBMIT
    guard.submit_flow_approved = True
    guard.submit_window.open(0)
    # A single approved submit may authorise its POST(s) — NOT an unbounded burst of
    # every POST the page fires during the window. After the request budget the
    # window closes and further mutating POSTs are refused (fail-closed).
    results = [guard.decide("POST", "https://app.example/x", now_ms=1) for _ in range(8)]
    assert any((not r.allow) and r.rule_id == "guard.submit.window_closed" for r in results)


def test_submit_respects_max_depth(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})

    async def fake_submit(*a, **k):
        ps = types.SimpleNamespace(location="https://app.example/deep")
        return SubmitResult(submitted=True, decision=None, confirmed=True,
                            outcome="navigation", page_state=ps)

    monkeypatch.setattr(crawler_mod, "execute_submit_phase_b", fake_submit)
    # An item AT max_depth still submits, but must NOT enqueue a deeper state.
    item = FrontierItem(url="https://app.example/form", depth=c._budget.max_depth)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Continue", "kind": "button"}],
                                        _fill("Continue"), "fp"))
    assert c._forms_submitted == 1
    assert c._frontier.pop() is None

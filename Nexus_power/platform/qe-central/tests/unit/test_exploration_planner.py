"""Caged exploration PLANNER (C) — the LLM proposes, the crawler disposes.

Pins the safety envelope of the plan the planner hands the explorer:
  * grounded — a pattern NOT present in a real discovered route is dropped;
  * bounded — weight clamped 1..3, at most 8 patterns, safe substring only;
  * fail-open — no LLM / bad JSON / no prior artifact ⇒ empty plan (byte-identical).
The explorer applies the plan as frontier priority ONLY (proven in the qe-explorer
suite), so a plan can never add a state, cross a submit, or leave the fence.
"""
from __future__ import annotations

import asyncio

import app.services.exploration_planner as planner
from app.clients.platform_api import LLMResult
from app.services.exploration_planner import _validate_plan, build_exploration_plan

_ROUTES = ["/quote", "/apply", "/review", "#/quote", "/privacy"]


# ── _validate_plan: grounding + bounding (pure) ──────────────────────────────


def test_validate_grounds_and_bounds():
    raw = (
        '{"priority_patterns": ['
        '{"pattern": "quote", "weight": 3, "reason": "money funnel"},'
        '{"pattern": "apply", "weight": 9},'          # weight clamped to 3
        '{"pattern": "checkout", "weight": 3},'       # NOT in routes → dropped
        '{"pattern": "quote"},'                       # duplicate → dropped
        '{"pattern": ".*evil", "weight": 2}'          # regex metachars → dropped
        ']}'
    )
    out = _validate_plan(raw, _ROUTES)["priority_patterns"]
    assert [(p["pattern"], p["weight"]) for p in out] == [("quote", 3), ("apply", 3)]


def test_validate_strips_code_fences_and_survives_garbage():
    assert _validate_plan("```json\n{\"priority_patterns\": []}\n```", _ROUTES) == {"priority_patterns": []}
    assert _validate_plan("not json at all", _ROUTES) == {"priority_patterns": []}
    assert _validate_plan('{"priority_patterns": "wrong-type"}', _ROUTES) == {"priority_patterns": []}


def test_validate_caps_at_eight_patterns():
    routes = [f"/sec{i}" for i in range(20)]
    raw = '{"priority_patterns": [' + ",".join(
        f'{{"pattern": "sec{i}", "weight": 2}}' for i in range(20)) + ']}'
    assert len(_validate_plan(raw, routes)["priority_patterns"]) == 8


# ── build_exploration_plan: fail-open orchestration ──────────────────────────


def _build(**patches):
    for name, fn in patches.items():
        setattr(planner, name, fn)
    return asyncio.run(build_exploration_plan("t1", "app1", "art1", "http://app"))


def test_no_prior_artifact_yields_empty_plan(monkeypatch):
    # a FIRST crawl has nothing to ground against → empty (byte-identical).
    out = asyncio.run(build_exploration_plan("t1", "app1", "", "http://app"))
    assert out == {"priority_patterns": []}


def test_llm_unavailable_fails_open(monkeypatch):
    async def _routes(_t, _a): return ["/quote"]
    async def _labels(_t, _a): return []
    async def _llm(**kw): return LLMResult(ok=False, detail="down")
    monkeypatch.setattr(planner, "known_routes_for_artifact", _routes)
    monkeypatch.setattr(planner, "known_labels_for_artifact", _labels)
    monkeypatch.setattr(planner.platform_api, "complete_llm", _llm)
    assert asyncio.run(build_exploration_plan("t1", "a", "art1", "http://app")) == {"priority_patterns": []}


def test_happy_path_builds_a_grounded_plan(monkeypatch):
    async def _routes(_t, _a): return ["/quote", "/apply", "/privacy"]
    async def _labels(_t, _a): return ["Age", "Coverage"]
    async def _llm(**kw):
        return LLMResult(ok=True, text='{"priority_patterns": ['
                         '{"pattern": "quote", "weight": 3},'
                         '{"pattern": "hack", "weight": 3}]}')  # 'hack' ungrounded → dropped
    monkeypatch.setattr(planner, "known_routes_for_artifact", _routes)
    monkeypatch.setattr(planner, "known_labels_for_artifact", _labels)
    monkeypatch.setattr(planner.platform_api, "complete_llm", _llm)
    out = asyncio.run(build_exploration_plan("t1", "a", "art1", "http://app"))
    assert out == {"priority_patterns": [{"pattern": "quote", "weight": 3, "reason": ""}]}


def test_planner_exception_never_propagates(monkeypatch):
    async def _boom(_t, _a): raise RuntimeError("db down")
    monkeypatch.setattr(planner, "known_routes_for_artifact", _boom)
    assert asyncio.run(build_exploration_plan("t1", "a", "art1", "http://app")) == {"priority_patterns": []}

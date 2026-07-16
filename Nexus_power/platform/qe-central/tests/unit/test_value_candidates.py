"""#2 Value-oracle PROVING wiring — candidate → human-confirmed → run contract.

Pins the fail-closed confirmation loop: crawl-classified candidates are surfaced
for review; a confirm is GROUNDED (source_hint must be a captured candidate
selector) and normalized by the SAME frozen ``_normalize_outcome`` the run
contract uses; the confirmed outcome round-trips ``value_oracle_contract``
unchanged — everything downstream (factory value_assertions → compiler →
value_oracle PROVEN assertion → frozen verdict reducer) is untouched machinery.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routers.apps as apps_mod
from app.routers.apps import (
    ValueCandidateConfirmIn,
    _outcomes_as_list,
    confirm_value_candidate,
    list_value_candidates,
)
from app.services.answer_key import value_oracle_contract

_USER = {"tenant_id": "t1", "sub": "founder@x", "role": "manager"}

_CANDIDATES = [
    {"label": "Monthly Premium", "source_hint": "#premium", "text": "$75.00",
     "value_type": "currency", "confidence": 0.9, "reason": "currency value"},
    {"label": "Decision", "source_hint": "#decision", "text": "Approved",
     "value_type": "decision", "confidence": 0.75, "reason": "decision value"},
]


def _fake_row(answer_key=None, artifact="art1", status="active"):
    return SimpleNamespace(status=status, latest_artifact_id=artifact,
                           answer_key=dict(answer_key or {}), updated_at=None)


def _patch(monkeypatch, row, candidates=None):
    @asynccontextmanager
    async def _session(_tenant):
        yield SimpleNamespace()

    async def _require(_s, _t, _a):
        return row

    async def _cands(_t, _a):
        return [dict(c) for c in (candidates if candidates is not None else _CANDIDATES)]

    monkeypatch.setattr(apps_mod, "tenant_scoped_qec_session", _session)
    monkeypatch.setattr(apps_mod, "_require_app", _require)
    monkeypatch.setattr(apps_mod, "value_candidates_for_artifact", _cands)


def _confirm(row, monkeypatch, **body):
    _patch(monkeypatch, row)
    payload = ValueCandidateConfirmIn(**body)
    return asyncio.run(confirm_value_candidate("app1", payload, user=_USER))


# ── _outcomes_as_list coercions ───────────────────────────────────────────────


def test_outcomes_list_coercions():
    assert _outcomes_as_list({}) == []
    # the flat-map form upgrades losslessly; the _raw sentinel is dropped.
    got = _outcomes_as_list({"outcomes": {"premium": 75, "_raw": "free text"}})
    assert got == [{"field": "premium", "expected": 75}]
    # the list form passes through.
    rec = {"field": "p", "expected": 1, "source_hint": "#x"}
    assert _outcomes_as_list({"outcomes": [rec]}) == [rec]


# ── confirm: grounded happy path ─────────────────────────────────────────────


def test_confirm_writes_a_grounded_outcome_into_the_answer_key(monkeypatch):
    row = _fake_row()
    out = _confirm(row, monkeypatch, field="monthly_premium",
                   expected="$75.00", source_hint="#premium")
    rec = out["confirmed"]
    assert rec["source_hint"] == "#premium"
    assert rec["match"] == "numeric" and rec["expected"] == 75.0  # frozen normalizer
    assert row.answer_key["outcomes"] == [rec]
    # ROUND-TRIP: the run contract carries it verbatim to the factory.
    contract = value_oracle_contract(row.answer_key)
    assert contract["outcomes"][0]["source_hint"] == "#premium"
    assert contract["outcomes"][0]["expected"] == 75.0


def test_reconfirming_the_same_field_and_hint_replaces_not_duplicates(monkeypatch):
    row = _fake_row()
    _confirm(row, monkeypatch, field="premium", expected="$75.00", source_hint="#premium")
    _confirm(row, monkeypatch, field="premium", expected="$80.00", source_hint="#premium")
    outs = row.answer_key["outcomes"]
    assert len(outs) == 1 and outs[0]["expected"] == 80.0


# ── confirm: fail-closed gates ───────────────────────────────────────────────


def test_confirm_preserves_the_raw_sentinel_and_foreign_entries(monkeypatch):
    """Adversarial regression: a confirm must never silently prune data it did not
    author — the portal's ``_raw`` free-text sentinel and unknown entries survive."""
    row = _fake_row(answer_key={"outcomes": {"_raw": "free text brief", "legacy": 5}})
    _confirm(row, monkeypatch, field="premium", expected="$75.00", source_hint="#premium")
    outs = row.answer_key["outcomes"]
    assert {"_raw": "free text brief"} in outs           # sentinel preserved
    assert {"field": "legacy", "expected": 5} in outs    # flat-map entry upgraded, kept
    assert any(o.get("source_hint") == "#premium" for o in outs if isinstance(o, dict))
    # and the contract projector still drops the sentinel from the RUN contract.
    assert all("_raw" not in str(o.get("field", ""))
               for o in value_oracle_contract(row.answer_key)["outcomes"])


def test_ungrounded_source_hint_is_refused_422(monkeypatch):
    row = _fake_row()
    with pytest.raises(HTTPException) as ei:
        _confirm(row, monkeypatch, field="premium", expected="75",
                 source_hint="#hand-typed-selector")
    assert ei.value.status_code == 422
    assert "ungrounded source_hint" in str(ei.value.detail)
    assert row.answer_key.get("outcomes") is None  # nothing written


def test_ungroundable_expected_is_refused_422(monkeypatch):
    row = _fake_row()
    with pytest.raises(HTTPException) as ei:
        _confirm(row, monkeypatch, field="premium", expected="   ",
                 source_hint="#premium")
    assert ei.value.status_code == 422


def test_confirm_without_a_crawl_artifact_is_409(monkeypatch):
    row = _fake_row(artifact="")
    _patch(monkeypatch, row)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(confirm_value_candidate(
            "app1",
            ValueCandidateConfirmIn(field="p", expected="1", source_hint="#premium"),
            user=_USER))
    assert ei.value.status_code == 409


# ── list: candidates surfaced with confirmed flags ───────────────────────────


def test_list_marks_already_confirmed_candidates(monkeypatch):
    row = _fake_row(answer_key={"outcomes": [
        {"field": "premium", "expected": 75, "source_hint": "#premium"}]})
    _patch(monkeypatch, row)
    out = asyncio.run(list_value_candidates("app1", user=_USER))
    by_hint = {c["source_hint"]: c for c in out["candidates"]}
    assert by_hint["#premium"]["confirmed"] is True
    assert by_hint["#decision"]["confirmed"] is False
    # the captured text rides along as a pre-fill hint (never auto-asserted).
    assert by_hint["#premium"]["text"] == "$75.00"

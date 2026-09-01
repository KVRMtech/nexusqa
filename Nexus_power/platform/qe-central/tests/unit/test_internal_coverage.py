"""P4 — the crawl coverage report survives the completion callback and lands on
the exploration ``stats`` JSONB, so the app UI can turn "why did the crawl only
reach 2 flows?" into a NAMED, seed-this-field remediation list.

The model uses ``extra="ignore"``; without an explicit ``coverage`` field the
report would be silently dropped. These tests pin that it is NOT dropped, while
genuinely-unknown keys still are."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import app.routers.internal as internal
from app.routers.internal import CompletionCallback, _app_answer_key

_COVERAGE = {
    "forms_found": 3,
    "forms_submitted": 0,
    "fields_inferred": ["Email Address", "Zip Code"],
    "fields_needing_seed": ["Social Security Number", "Policy Number"],
    "submit_candidates": ["Continue", "Get a Quote"],
    "summary": "3 forms found; 2 fields need a client seed to go deeper.",
}


def test_callback_carries_coverage_not_dropped_by_extra_ignore():
    body = CompletionCallback.model_validate(
        {"tenant_id": "t", "exploration_id": "e", "crawl_id": "c", "coverage": _COVERAGE}
    )
    assert body.coverage == _COVERAGE
    assert body.coverage["fields_needing_seed"] == [
        "Social Security Number",
        "Policy Number",
    ]


def test_coverage_defaults_none_and_unknown_extra_still_ignored():
    body = CompletionCallback.model_validate(
        {"tenant_id": "t", "exploration_id": "e", "crawl_id": "c", "some_bogus_key": 1}
    )
    assert body.coverage is None
    assert not hasattr(body, "some_bogus_key")


def test_empty_coverage_is_falsy_so_stats_stays_clean():
    # section-7 guard is ``if body.coverage`` — None/{} must not write a stats key.
    for empty in (None, {}):
        body = CompletionCallback.model_validate(
            {"tenant_id": "t", "exploration_id": "e", "crawl_id": "c", "coverage": empty}
        )
        assert not body.coverage


# ── AUTO-GENERATE on crawl completion (the "crawl completes but nothing shows"
#    showstopper): the callback seeds generate with the app's value-oracle
#    contract, and a read failure degrades to a body-less generate. ────────────


def _patch_app_row(monkeypatch, row):
    @asynccontextmanager
    async def _session(_tenant):
        yield SimpleNamespace()

    async def _execute_returns(_self):  # unused; kept for clarity
        return None

    class _Res:
        def scalar_one_or_none(self):
            return row

    class _Sess:
        async def execute(self, *a, **k):
            return _Res()

    @asynccontextmanager
    async def _real_session(_tenant):
        yield _Sess()

    monkeypatch.setattr(internal, "tenant_scoped_qec_session", _real_session)


def test_app_answer_key_projects_value_oracle_contract(monkeypatch):
    row = SimpleNamespace(answer_key={"outcomes": {"premium": 75.0},
                                      "fill": {"Age": "40"}, "rules": []})
    _patch_app_row(monkeypatch, row)
    out = asyncio.run(_app_answer_key("t1", "app1"))
    # only outcomes/rules survive to the factory (fills are crawl-side already).
    assert "outcomes" in out and "rules" in out
    assert out["outcomes"][0]["field"] == "premium"
    assert "fill" not in out


def test_app_answer_key_is_empty_without_app(monkeypatch):
    assert asyncio.run(_app_answer_key("t1", "")) == {}
    _patch_app_row(monkeypatch, None)     # app row missing
    assert asyncio.run(_app_answer_key("t1", "gone")) == {}

"""QE-Central Phase-5 — per-band autonomy TREND tests (pure + DB-gated).

Pins the honesty of :func:`app.services.touch_meter.autonomy_trend` /
:func:`_assemble_trend`:

  * the trend is PER band PER cycle — it DELIBERATELY never emits a single
    averaged autonomy number (no scalar aggregate anywhere in the payload);
  * a band with NO governed denominator in a cycle is reported as ``null``
    (``autonomy_pct is None``) — NEVER 100%;
  * a governed band with ZERO touches in a cycle reads a genuine 100% (distinct
    from the null above);
  * ingested audit touches with no band land in their OWN ``unattributed``
    series — never folded into a real band's numerator;
  * a band neither governed nor touched anywhere in the window is omitted.

Pure ``_assemble_trend`` needs no I/O; the DB path (cycle window from
``app_cycles`` + touches join) is skipif-gated on ``QEC_TEST_DATABASE_URL``.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import touch_meter
from app.services.touch_meter import (
    BAND_UNATTRIBUTED,
    MODEL_VERSION,
    _assemble_trend,
)
from app.db import utc_now


def _cycles(*ids) -> list[dict]:
    return [{"cycle_id": c, "state": "done"} for c in ids]


# ══════════════════════ pure: _assemble_trend ═════════════════════════════

def test_trend_is_per_band_per_cycle_never_a_single_number():
    out = _assemble_trend(
        app_id="a1",
        cycles_meta=_cycles("c1", "c2"),
        governed_by_band={"P0": 4, "P1": 2},
        touches_by_cycle_band={"c2": {"P0": 1}},
    )
    # No scalar aggregate autonomy figure exists anywhere at the top level.
    assert "autonomy_pct" not in out
    assert "autonomy" not in out
    assert isinstance(out["per_band"], dict)
    assert all(isinstance(v, list) for v in out["per_band"].values())
    assert out["window"] == 2
    assert out["model_version"] == MODEL_VERSION
    assert "per band" in out["note"].lower()

    p0 = out["per_band"]["P0"]
    assert [e["cycle_id"] for e in p0] == ["c1", "c2"]      # chronological
    assert p0[0]["autonomy_pct"] == 100.0                    # 4 governed, 0 touches
    assert p0[1]["autonomy_pct"] == 75.0                     # 4 governed, 1 touch
    # P1 governed with zero touches → a genuine 100% both cycles.
    assert [e["autonomy_pct"] for e in out["per_band"]["P1"]] == [100.0, 100.0]


def test_empty_band_is_null_not_100_percent():
    # P0 has NO governed scenarios but WAS touched → it appears, but every cycle's
    # autonomy is null (not computable), never a misleading 100%.
    out = _assemble_trend(
        app_id="a1",
        cycles_meta=_cycles("c1", "c2"),
        governed_by_band={"P1": 3},                 # P0 governed == 0
        touches_by_cycle_band={"c1": {"P0": 2}},
    )
    assert "P0" in out["per_band"]
    for entry in out["per_band"]["P0"]:
        assert entry["governed_scenarios"] == 0
        assert entry["autonomy_pct"] is None        # null, NEVER 100.0
        assert entry["autonomy_pct"] != 100.0


def test_unattributed_touches_are_never_folded_into_a_real_band():
    out = _assemble_trend(
        app_id="a1",
        cycles_meta=_cycles("c1"),
        governed_by_band={"P0": 1},
        touches_by_cycle_band={"c1": {BAND_UNATTRIBUTED: 3}},
    )
    # The real P0 band saw ZERO human touches (100% autonomous that cycle)…
    assert out["per_band"]["P0"][0]["human_touches"] == 0
    assert out["per_band"]["P0"][0]["autonomy_pct"] == 100.0
    # …and the unattributed touches surface in their OWN series.
    assert out["unattributed"] == [{"cycle_id": "c1", "human_touches": 3}]
    # 'unattributed' is not a band and never becomes a per_band row.
    assert BAND_UNATTRIBUTED not in out["per_band"]


def test_band_neither_governed_nor_touched_is_omitted():
    out = _assemble_trend(
        app_id="a1",
        cycles_meta=_cycles("c1"),
        governed_by_band={"P0": 1},
        touches_by_cycle_band={},
    )
    assert set(out["per_band"].keys()) == {"P0"}      # P1/P2/P3 omitted (noise)
    assert out["unattributed"] == []


def test_touches_reduce_the_band_autonomy_per_cycle():
    out = _assemble_trend(
        app_id="a1",
        cycles_meta=_cycles("c1", "c2", "c3"),
        governed_by_band={"P0": 2},
        touches_by_cycle_band={"c1": {"P0": 0}, "c2": {"P0": 1}, "c3": {"P0": 2}},
    )
    assert [e["autonomy_pct"] for e in out["per_band"]["P0"]] == [100.0, 50.0, 0.0]


# ══════════════════════ DB path (skipif-gated) ════════════════════════════

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the trend cycle-window join needs a "
           "disposable Postgres (QecBase tables are created in-test)",
)


@asynccontextmanager
async def _scoped(factory, tenant):
    session = factory()
    try:
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
            {"t": tenant},
        )
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@needs_db
def test_autonomy_trend_windows_cycles_and_attributes_touches():
    asyncio.run(_run_trend())


async def _run_trend():
    from app.db.controlplane_models import AppCycleRow
    from app.db.gov_models import ScenarioRow, TouchEventRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-trend-{uuid.uuid4().hex[:10]}"
    now = utc_now()
    original = touch_meter.tenant_scoped_qec_session
    try:
        touch_meter.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)
        async with _scoped(factory, tenant) as s:
            # Two active P0 scenarios (the governed denominator).
            for i in (1, 2):
                s.add(ScenarioRow(
                    scenario_id=f"sc{i}", tenant_id=tenant, app_id="a1",
                    criticality_band="P0", status="active",
                ))
            # Two cycles: c1 older, c2 newer.
            s.add(AppCycleRow(cycle_id="c1", tenant_id=tenant, app_id="a1",
                              trigger="manual", state="done",
                              created_at=now - timedelta(hours=2)))
            s.add(AppCycleRow(cycle_id="c2", tenant_id=tenant, app_id="a1",
                              trigger="manual", state="done",
                              created_at=now - timedelta(hours=1)))
            # One P0 human touch, attributed to c2 only.
            s.add(TouchEventRow(
                touch_id=uuid.uuid4().hex, tenant_id=tenant, app_id="a1",
                touch_type=touch_meter.TOUCH_SCENARIO_APPROVE, band="P0",
                cycle_id="c2", source=touch_meter.SOURCE_QEC_DIRECT,
                actor="jane", created_at=now,
            ))

        trend = await touch_meter.autonomy_trend(
            tenant_id=tenant, app_id="a1", cycles=10,
        )
        assert trend["window"] == 2
        assert [c["cycle_id"] for c in trend["cycles"]] == ["c1", "c2"]  # chronological
        p0 = trend["per_band"]["P0"]
        by_cycle = {e["cycle_id"]: e for e in p0}
        # c1: 2 governed, 0 touches → 100% autonomous.
        assert by_cycle["c1"]["autonomy_pct"] == 100.0
        # c2: 2 governed, 1 touch → 50%.
        assert by_cycle["c2"]["human_touches"] == 1
        assert by_cycle["c2"]["autonomy_pct"] == 50.0
        # Still no single averaged number.
        assert "autonomy_pct" not in trend
    finally:
        touch_meter.tenant_scoped_qec_session = original
        await engine.dispose()

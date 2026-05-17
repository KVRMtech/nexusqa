"""
Contract tests for QueueSaturationGuard (architect P2).

Pins the invariants the upload-gate must hold:

  - Below threshold on every protected lane → allow.
  - Any lane above threshold → reject + report which lanes are over.
  - Priority-tier uploads bypass the standard-lane gate.
  - Redis error → fail-open (allow). Rejecting every upload because
    the metrics shard is briefly unreachable would be worse than a
    short over-shoot.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "sdk" / "nexus-sdk"))

from nexus_sdk.workflows.admission import QueueSaturationGuard  # noqa: E402


class _FakeRedis:
    def __init__(self, depths: dict[str, int]) -> None:
        # depths keyed by full stream key "nexus:queue:<lane>".
        self._depths = depths
        self.fail = False

    async def xlen(self, key: str) -> int:
        if self.fail:
            raise RuntimeError("redis blip")
        return int(self._depths.get(key, 0))


@pytest.mark.asyncio
async def test_allows_when_all_lanes_under_threshold():
    r = _FakeRedis({
        "nexus:queue:eyes.cpu": 10,
        "nexus:queue:spine.cpu": 20,
        "nexus:queue:eyes.gpu": 0,
    })
    g = QueueSaturationGuard(redis_client=r, threshold=100)
    allowed, sat, retry = await g.check()
    assert allowed is True
    assert sat is None
    assert retry == 0


@pytest.mark.asyncio
async def test_rejects_when_any_lane_over_threshold():
    r = _FakeRedis({
        "nexus:queue:eyes.cpu": 50,
        "nexus:queue:spine.cpu": 5000,   # saturated
        "nexus:queue:eyes.gpu": 0,
    })
    g = QueueSaturationGuard(redis_client=r, threshold=100, retry_after_seconds=45)
    allowed, sat, retry = await g.check()
    assert allowed is False
    assert sat == {"spine.cpu": 5000}    # only the over-threshold lane(s)
    assert retry == 45


@pytest.mark.asyncio
async def test_rejects_when_multiple_lanes_over_threshold():
    r = _FakeRedis({
        "nexus:queue:eyes.cpu": 500,
        "nexus:queue:spine.cpu": 700,
        "nexus:queue:eyes.gpu": 50,
    })
    g = QueueSaturationGuard(redis_client=r, threshold=200)
    allowed, sat, _ = await g.check()
    assert allowed is False
    assert set(sat.keys()) == {"eyes.cpu", "spine.cpu"}
    assert sat["eyes.cpu"] == 500
    assert sat["spine.cpu"] == 700


@pytest.mark.asyncio
async def test_priority_uploads_bypass_gate():
    """Premium tenants ride the .priority lanes — a saturated standard
    queue must not bounce them."""
    r = _FakeRedis({
        "nexus:queue:eyes.cpu": 10_000,
        "nexus:queue:spine.cpu": 10_000,
        "nexus:queue:eyes.gpu": 10_000,
    })
    g = QueueSaturationGuard(redis_client=r, threshold=100)
    allowed, sat, _ = await g.check(is_priority=True)
    assert allowed is True
    assert sat is None


@pytest.mark.asyncio
async def test_fail_open_on_redis_error():
    r = _FakeRedis({})
    r.fail = True
    g = QueueSaturationGuard(redis_client=r, threshold=100)
    allowed, sat, _ = await g.check()
    assert allowed is True
    assert sat is None


@pytest.mark.asyncio
async def test_disabled_when_no_redis():
    g = QueueSaturationGuard(redis_client=None, threshold=100)
    assert g.enabled is False
    allowed, _, _ = await g.check()
    assert allowed is True


@pytest.mark.asyncio
async def test_disabled_when_threshold_zero():
    r = _FakeRedis({"nexus:queue:eyes.cpu": 999_999})
    g = QueueSaturationGuard(redis_client=r, threshold=0)
    assert g.enabled is False
    allowed, _, _ = await g.check()
    assert allowed is True


@pytest.mark.asyncio
async def test_custom_lanes():
    r = _FakeRedis({
        "nexus:queue:eyes.cpu": 0,        # under threshold but not checked
        "nexus:queue:custom.lane": 500,   # over threshold AND checked
    })
    g = QueueSaturationGuard(
        redis_client=r, threshold=100, lanes=("custom.lane",),
    )
    allowed, sat, _ = await g.check()
    assert allowed is False
    assert sat == {"custom.lane": 500}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""ChannelPolicyService — decision logic (pure, no DB)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.policy import (
    PolicyContext,
    PolicyDecision,
    _evaluate_row,
)


def _row(**kwargs) -> dict:
    base = {
        "echo_mode": "inherit",
        "min_confidence_override": None,
        "allowlist_json": {},
        "blocklist_json": {},
        "quiet_hours_json": {},
        "metadata_json": {},
    }
    base.update(kwargs)
    return base


def _ctx(**kwargs) -> PolicyContext:
    base = {
        "tenant_id": "t1",
        "surface": "slack",
        "channel_id_ext": "C1",
        "product_ids": (),
        "topic_hints": (),
        "now": datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc),
    }
    base.update(kwargs)
    return PolicyContext(**base)


# ── echo_mode ──────────────────────────────────────────────────


def test_muted_disallows() -> None:
    d = _evaluate_row(_row(echo_mode="muted"), _ctx())
    assert d.allow is False
    assert d.reason == "channel_muted"


def test_inherit_passes_no_override() -> None:
    d = _evaluate_row(_row(echo_mode="inherit"), _ctx())
    assert d.allow is True
    assert d.forced_mode is None


def test_forced_mode_takes_effect() -> None:
    d = _evaluate_row(_row(echo_mode="dm_only"), _ctx())
    assert d.allow is True
    assert d.forced_mode == "dm_only"


# ── min_confidence override ────────────────────────────────────


def test_min_confidence_override_returned() -> None:
    d = _evaluate_row(_row(min_confidence_override=0.92), _ctx())
    assert d.min_confidence_override == pytest.approx(0.92)


# ── allowlist / blocklist ──────────────────────────────────────


def test_blocklist_product_blocks() -> None:
    d = _evaluate_row(
        _row(blocklist_json={"products": ["pii-confidential"]}),
        _ctx(product_ids=("pii-confidential", "lt5")),
    )
    assert d.allow is False
    assert d.reason == "product_blocklisted"


def test_blocklist_topic_blocks_case_insensitive() -> None:
    d = _evaluate_row(
        _row(blocklist_json={"topics": ["HR/Compensation"]}),
        _ctx(topic_hints=("hr/compensation",)),
    )
    assert d.allow is False
    assert d.reason == "topic_blocklisted"


def test_allowlist_blocks_when_no_match() -> None:
    d = _evaluate_row(
        _row(allowlist_json={"products": ["lt5"]}),
        _ctx(product_ids=("wl3",)),
    )
    assert d.allow is False
    assert d.reason == "product_not_in_allowlist"


def test_allowlist_passes_when_match() -> None:
    d = _evaluate_row(
        _row(allowlist_json={"products": ["lt5"]}),
        _ctx(product_ids=("lt5",)),
    )
    assert d.allow is True


def test_empty_allowlist_does_not_block() -> None:
    d = _evaluate_row(
        _row(allowlist_json={"products": []}),
        _ctx(product_ids=("anything",)),
    )
    assert d.allow is True


# ── quiet hours ────────────────────────────────────────────────


def test_quiet_hours_blocks_inside_window() -> None:
    # Use UTC so the test is robust against hosts without tzdata.
    # Wed 2026-05-13 14:30 UTC → window 14:00-15:00 → blocked.
    d = _evaluate_row(
        _row(
            quiet_hours_json={
                "timezone": "UTC",
                "windows": [
                    {"days": ["wed"], "start": "14:00", "end": "15:00"}
                ],
            }
        ),
        _ctx(),
    )
    assert d.allow is False
    assert d.reason.startswith("quiet_hours:")


def test_quiet_hours_passes_outside_window() -> None:
    d = _evaluate_row(
        _row(
            quiet_hours_json={
                "timezone": "UTC",
                "windows": [{"start": "00:00", "end": "06:00"}],
            }
        ),
        _ctx(),  # 14:30 UTC — outside window
    )
    assert d.allow is True


def test_overnight_quiet_hours_block_at_2am() -> None:
    d = _evaluate_row(
        _row(
            quiet_hours_json={
                "timezone": "UTC",
                "windows": [{"start": "22:00", "end": "06:00"}],
            }
        ),
        _ctx(now=datetime(2026, 5, 13, 2, 0, tzinfo=timezone.utc)),
    )
    assert d.allow is False


def test_quiet_hours_day_filter_skips_off_days() -> None:
    d = _evaluate_row(
        _row(
            quiet_hours_json={
                "timezone": "UTC",
                "windows": [
                    {"days": ["sat", "sun"], "start": "00:00", "end": "23:59"}
                ],
            }
        ),
        # Wed
        _ctx(now=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)),
    )
    assert d.allow is True

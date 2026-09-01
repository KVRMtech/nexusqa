"""TourComposer — atlas-walking playlist generator.

Imports happen inside test functions so the platform-services conftest's
module-isolation fixture can swap the ``app`` package without colliding
with sibling service tests.
"""

from __future__ import annotations

from typing import Any

import pytest


def _node(
    *,
    nid: str,
    layer: str,
    label: str,
    confidence: float = 0.9,
    segments: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "atlas_node_id": nid,
        "layer": layer,
        "label": label,
        "confidence": confidence,
        "source_segment_ids": list(segments),
        "metadata_json": {},
    }


def test_empty_nodes_returns_empty_tour() -> None:
    from app.actions import TourComposer, TourComposerConfig

    composer = TourComposer(TourComposerConfig())
    tour = composer.compose(nodes=[], persona=None, target_minutes=None)
    assert tour.playlist == ()
    assert tour.atlas_node_ids == ()


def test_persona_order_drives_layer_priority() -> None:
    from app.actions import TourComposer

    composer = TourComposer()
    nodes = [
        _node(nid="x1", layer="experience", label="Quote form"),
        _node(nid="a1", layer="application", label="POST /quote"),
        _node(nid="r1", layer="rule", label="Tobacco 24-month lookback"),
        _node(nid="t1", layer="test", label="quote-ca-tobacco test"),
    ]
    qa = composer.compose(
        nodes=nodes, persona="qa", target_minutes=None
    )
    # QA persona prefers test → rule first.
    assert qa.playlist[0].atlas_node_id == "t1"
    assert qa.playlist[1].atlas_node_id == "r1"

    sales = composer.compose(
        nodes=nodes, persona="sales", target_minutes=None
    )
    # Sales persona prefers experience → rule.
    assert sales.playlist[0].atlas_node_id == "x1"


def test_unknown_persona_uses_default_order() -> None:
    from app.actions import TourComposer

    composer = TourComposer()
    nodes = [
        _node(nid="d1", layer="data", label="rate_tables"),
        _node(nid="x1", layer="experience", label="Quote form"),
    ]
    tour = composer.compose(
        nodes=nodes, persona="astrologer", target_minutes=None
    )
    # Default order is experience first.
    assert tour.playlist[0].atlas_node_id == "x1"


def test_confidence_breaks_tie_within_layer() -> None:
    from app.actions import TourComposer

    composer = TourComposer()
    nodes = [
        _node(nid="lo", layer="rule", label="A", confidence=0.7),
        _node(nid="hi", layer="rule", label="B", confidence=0.95),
    ]
    tour = composer.compose(
        nodes=nodes, persona="compliance", target_minutes=None
    )
    assert tour.playlist[0].atlas_node_id == "hi"
    assert tour.playlist[1].atlas_node_id == "lo"


def test_target_minutes_caps_total_seconds() -> None:
    from app.actions import TourComposer, TourComposerConfig

    composer = TourComposer(TourComposerConfig(default_segment_seconds=60))
    nodes = [
        _node(nid=f"n{i}", layer="experience", label=f"Step {i}")
        for i in range(20)
    ]
    tour = composer.compose(
        nodes=nodes, persona=None, target_minutes=3
    )
    # 3 minutes target × 60 seconds with a 60-second default. Each
    # segment also adds tokens (~2 tokens × 0.6 = 1.2s ≈ 61s), giving
    # roughly 3 segments worth of budget. The composer guarantees at
    # least minimum_segments (3) regardless.
    assert tour.coverage["estimated_seconds"] <= 60 * 3 + 60
    assert len(tour.playlist) >= 3


def test_segment_ids_pass_through_from_atlas_node() -> None:
    from app.actions import TourComposer

    composer = TourComposer()
    nodes = [
        _node(
            nid="n1",
            layer="experience",
            label="Quote",
            segments=("seg-1", "seg-2"),
        )
    ]
    tour = composer.compose(
        nodes=nodes, persona=None, target_minutes=None
    )
    assert tour.playlist[0].segment_ids == ["seg-1", "seg-2"]


def test_coverage_records_layers_used() -> None:
    from app.actions import TourComposer

    composer = TourComposer()
    nodes = [
        _node(nid="x1", layer="experience", label="A"),
        _node(nid="a1", layer="application", label="B"),
        _node(nid="r1", layer="rule", label="C"),
    ]
    tour = composer.compose(
        nodes=nodes, persona=None, target_minutes=None
    )
    assert set(tour.coverage["layers_covered"]) == {
        "experience", "application", "rule"
    }
    assert tour.coverage["layer_counts"]["experience"] == 1


def test_maximum_segments_cap() -> None:
    from app.actions import TourComposer, TourComposerConfig

    composer = TourComposer(TourComposerConfig(maximum_segments=4))
    nodes = [
        _node(nid=f"n{i}", layer="experience", label=f"X{i}")
        for i in range(20)
    ]
    tour = composer.compose(nodes=nodes, persona=None, target_minutes=None)
    assert len(tour.playlist) == 4


def test_invalid_config_rejected() -> None:
    from app.actions import TourComposerConfig

    with pytest.raises(ValueError):
        TourComposerConfig(min_segment_seconds=0)
    with pytest.raises(ValueError):
        TourComposerConfig(min_segment_seconds=30, max_segment_seconds=10)
    with pytest.raises(ValueError):
        TourComposerConfig(default_segment_seconds=5, min_segment_seconds=30)

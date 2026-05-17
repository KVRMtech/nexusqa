"""Synthesized tour composer.

Walks the atlas for a given product and returns a persona-targeted
playlist of atlas nodes (with their evidence segments). The composer
is pure — given a list of atlas-node dicts, it sorts, filters, and
budgets without touching the network or the database.

Production wiring (in the route handler):

    nodes = await atlas_repo.list_nodes(...)
    composer = TourComposer()
    tour = composer.compose(nodes=nodes, persona="qa", target_minutes=15)
    await action_repo.save_tour(...)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .models import TourSegment

logger = logging.getLogger(__name__)


# ── Persona → layer ordering ───────────────────────────────────


_DEFAULT_LAYER_ORDER = (
    "experience",
    "application",
    "data",
    "rule",
    "test",
    "ops",
    "compliance",
)


_PERSONA_LAYER_ORDERS: dict[str, tuple[str, ...]] = {
    "engineer": (
        "application",
        "data",
        "experience",
        "rule",
        "test",
        "ops",
        "compliance",
    ),
    "backend": (
        "application",
        "data",
        "rule",
        "experience",
        "test",
        "ops",
        "compliance",
    ),
    "qa": (
        "test",
        "rule",
        "experience",
        "application",
        "data",
        "ops",
        "compliance",
    ),
    "sales": (
        "experience",
        "rule",
        "compliance",
        "application",
        "data",
        "ops",
        "test",
    ),
    "compliance": (
        "compliance",
        "rule",
        "data",
        "application",
        "experience",
        "test",
        "ops",
    ),
    "operations": (
        "ops",
        "application",
        "data",
        "experience",
        "rule",
        "compliance",
        "test",
    ),
    "trainer": (
        "experience",
        "rule",
        "application",
        "data",
        "compliance",
        "test",
        "ops",
    ),
}


# ── DTOs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TourComposerConfig:
    default_segment_seconds: int = 30
    min_segment_seconds: int = 10
    max_segment_seconds: int = 120
    label_token_seconds: float = 0.6  # heuristic — ~100 wpm read
    minimum_segments: int = 3
    maximum_segments: int = 80

    def __post_init__(self) -> None:
        if self.min_segment_seconds <= 0:
            raise ValueError("min_segment_seconds must be > 0")
        if self.max_segment_seconds < self.min_segment_seconds:
            raise ValueError(
                "max_segment_seconds must be >= min_segment_seconds"
            )
        if self.default_segment_seconds < self.min_segment_seconds:
            raise ValueError(
                "default_segment_seconds must be >= min_segment_seconds"
            )


@dataclass(frozen=True)
class ComposedTour:
    playlist: tuple[TourSegment, ...]
    coverage: dict[str, Any]
    atlas_node_ids: tuple[str, ...]


# ── Composer ────────────────────────────────────────────────────


class TourComposer:
    def __init__(self, config: Optional[TourComposerConfig] = None):
        self._cfg = config or TourComposerConfig()

    def compose(
        self,
        *,
        nodes: Iterable[dict[str, Any]],
        persona: Optional[str],
        target_minutes: Optional[int],
    ) -> ComposedTour:
        all_nodes = list(nodes)
        if not all_nodes:
            return ComposedTour(playlist=(), coverage={}, atlas_node_ids=())

        layer_order = _PERSONA_LAYER_ORDERS.get(
            (persona or "").lower(), _DEFAULT_LAYER_ORDER
        )
        by_layer: dict[str, list[dict[str, Any]]] = {}
        for n in all_nodes:
            by_layer.setdefault(n.get("layer", ""), []).append(n)

        # Within a layer, rank by (confidence desc, source_count desc, label asc).
        for layer in by_layer:
            by_layer[layer].sort(
                key=lambda r: (
                    -float(r.get("confidence") or 0.0),
                    -_source_count(r),
                    str(r.get("label") or ""),
                )
            )

        # Budget: total seconds allowed by target_minutes (None → just
        # cap at maximum_segments and let durations decide).
        budget_seconds: Optional[int] = (
            int(target_minutes) * 60 if target_minutes else None
        )

        chosen: list[TourSegment] = []
        used_layers: set[str] = set()
        spent = 0
        ordinal = 0

        # First pass — round-robin across layers in persona order so
        # short tours still touch the most relevant layers.
        max_segments = self._cfg.maximum_segments
        layer_cursors = {layer: 0 for layer in layer_order}
        added_in_pass = True
        while added_in_pass and len(chosen) < max_segments:
            added_in_pass = False
            for layer in layer_order:
                candidates = by_layer.get(layer) or []
                cursor = layer_cursors.get(layer, 0)
                if cursor >= len(candidates):
                    continue
                row = candidates[cursor]
                seconds = self._estimate_seconds(row)
                if budget_seconds is not None and spent + seconds > budget_seconds:
                    # Try to squeeze a smaller segment in only if we
                    # haven't met the minimum count yet.
                    if len(chosen) >= self._cfg.minimum_segments:
                        layer_cursors[layer] = cursor + 1
                        continue
                    # Trim to minimum segment length so the floor case
                    # still produces something.
                    seconds = self._cfg.min_segment_seconds
                chosen.append(
                    _to_segment(row, ordinal=ordinal, seconds=seconds)
                )
                used_layers.add(layer)
                spent += seconds
                ordinal += 1
                layer_cursors[layer] = cursor + 1
                added_in_pass = True
                if len(chosen) >= max_segments:
                    break
                if (
                    budget_seconds is not None
                    and spent >= budget_seconds
                    and len(chosen) >= self._cfg.minimum_segments
                ):
                    break
            if (
                budget_seconds is not None
                and spent >= budget_seconds
                and len(chosen) >= self._cfg.minimum_segments
            ):
                break

        coverage = {
            "layers_covered": sorted(used_layers),
            "layer_counts": {
                layer: sum(1 for c in chosen if c.layer == layer)
                for layer in used_layers
            },
            "estimated_seconds": spent,
        }
        return ComposedTour(
            playlist=tuple(chosen),
            coverage=coverage,
            atlas_node_ids=tuple(c.atlas_node_id for c in chosen),
        )

    # ── Internals ──────────────────────────────────────────────

    def _estimate_seconds(self, row: dict[str, Any]) -> int:
        label = str(row.get("label") or "")
        token_count = len(re.findall(r"\w+", label))
        seconds = int(round(
            self._cfg.default_segment_seconds
            + token_count * self._cfg.label_token_seconds
        ))
        return max(
            self._cfg.min_segment_seconds,
            min(self._cfg.max_segment_seconds, seconds),
        )


# ── Helpers ─────────────────────────────────────────────────────


def _source_count(row: dict[str, Any]) -> int:
    return len(row.get("source_segment_ids") or [])


def _to_segment(
    row: dict[str, Any], *, ordinal: int, seconds: int
) -> TourSegment:
    return TourSegment(
        atlas_node_id=str(row.get("atlas_node_id") or ""),
        label=str(row.get("label") or row.get("atlas_node_id") or "(unlabelled)"),
        layer=str(row.get("layer") or ""),
        segment_ids=list(row.get("source_segment_ids") or []),
        speaker_id=(
            (row.get("metadata_json") or {}).get("speaker_id")
            if isinstance(row.get("metadata_json"), dict)
            else None
        ),
        estimated_seconds=int(seconds),
        ordinal=int(ordinal),
    )

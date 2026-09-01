"""Impact analyzer BFS — in-memory session that pattern-matches
``atlas_nodes`` vs ``atlas_edges`` queries.

Now that the analyzer issues exactly one SELECT against each table the
fake session can return the full row set for whichever one was asked
for, and the analyzer indexes by ``atlas_node_id`` in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pytest


# ── In-memory session fake ─────────────────────────────────────


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Async session that distinguishes nodes vs edges queries by the
    table name appearing in the compiled SQL.  Accepts the extra
    positional/keyword arg that SQLAlchemy passes for bound parameters
    (the ``set_config`` call uses one)."""

    def __init__(
        self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
    ):
        self._nodes = nodes
        self._edges = edges

    async def execute(self, stmt, *args, **kwargs):  # noqa: ARG002
        text = str(stmt).lower()
        if "atlas_edges" in text:
            return _Result(self._edges)
        if "atlas_nodes" in text:
            return _Result(self._nodes)
        # set_config / other text statements are no-ops.
        return _Result([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@dataclass
class _FakeSessionFactory:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]

    def __call__(self):
        return _FakeSession(self.nodes, self.edges)


# ── Helpers ────────────────────────────────────────────────────


def _node(nid: str, layer: str, label: str = "n") -> dict[str, Any]:
    return {
        "atlas_node_id": nid,
        "tenant_id": "t1",
        "product_id": "p_lt5",
        "node_type": "TranscriptSegment",
        "layer": layer,
        "label": label,
        "confidence": 0.9,
    }


def _edge(
    *,
    fr: str,
    to: str,
    rel: str = "calls_api",
    status: str = "auto",
) -> dict[str, Any]:
    return {
        "edge_id": f"{fr}->{to}",
        "tenant_id": "t1",
        "product_id": "p_lt5",
        "from_atlas_node_id": fr,
        "to_atlas_node_id": to,
        "relation_type": rel,
        "confidence": 0.95,
        "status": status,
    }


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyzer_returns_none_for_unknown_root() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    nodes = [_node("ui-1", "experience")]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, []), ImpactAnalyzerConfig()
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="missing"
    )
    assert report is None


@pytest.mark.asyncio
async def test_analyzer_walks_downstream_and_upstream() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    nodes = [
        _node("ui-1", "experience", label="Quote button"),
        _node("api-1", "application", label="POST /quote"),
        _node("db-1", "data", label="quotes table"),
        _node("rule-1", "rule", label="tobacco lookback"),
        _node("test-1", "test", label="quote-ca-test"),
    ]
    edges = [
        _edge(fr="ui-1", to="api-1", rel="calls_api"),
        _edge(fr="api-1", to="db-1", rel="reads_table"),
        _edge(fr="api-1", to="rule-1", rel="enforces_rule"),
        _edge(fr="test-1", to="rule-1", rel="tests_rule"),
    ]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, edges),
        ImpactAnalyzerConfig(max_depth=3),
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="ui-1"
    )
    assert report is not None
    downstream_ids = {n.atlas_node_id for n in report.downstream}
    assert {"api-1", "db-1", "rule-1"}.issubset(downstream_ids)
    # Upstream from ui-1: nothing points INTO ui-1.
    assert report.upstream == ()
    assert report.blast_radius == len(downstream_ids)


@pytest.mark.asyncio
async def test_analyzer_finds_upstream_from_rule() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    nodes = [
        _node("ui-1", "experience"),
        _node("api-1", "application"),
        _node("rule-1", "rule"),
        _node("test-1", "test"),
    ]
    edges = [
        _edge(fr="ui-1", to="api-1"),
        _edge(fr="api-1", to="rule-1", rel="enforces_rule"),
        _edge(fr="test-1", to="rule-1", rel="tests_rule"),
    ]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, edges),
        ImpactAnalyzerConfig(max_depth=3),
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="rule-1"
    )
    assert report is not None
    upstream_ids = {n.atlas_node_id for n in report.upstream}
    assert {"api-1", "test-1"}.issubset(upstream_ids)


@pytest.mark.asyncio
async def test_analyzer_excludes_rejected_edges_by_default() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    nodes = [_node("ui-1", "experience"), _node("api-1", "application")]
    edges = [_edge(fr="ui-1", to="api-1", status="rejected")]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, edges), ImpactAnalyzerConfig()
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="ui-1"
    )
    assert report is not None
    assert report.downstream == ()


@pytest.mark.asyncio
async def test_analyzer_includes_rejected_when_opted_in() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    nodes = [_node("ui-1", "experience"), _node("api-1", "application")]
    edges = [_edge(fr="ui-1", to="api-1", status="rejected")]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, edges),
        ImpactAnalyzerConfig(include_rejected=True),
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="ui-1"
    )
    assert report is not None
    assert {n.atlas_node_id for n in report.downstream} == {"api-1"}


@pytest.mark.asyncio
async def test_analyzer_marks_pending_review_in_layer_summary() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    nodes = [
        _node("ui-1", "experience"),
        _node("api-1", "application"),
        _node("db-1", "data"),
    ]
    edges = [
        _edge(fr="ui-1", to="api-1", status="auto"),
        _edge(fr="api-1", to="db-1", status="pending_review"),
    ]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, edges), ImpactAnalyzerConfig()
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="ui-1"
    )
    assert report is not None
    assert report.layer_summary["data"].has_pending_review is True
    assert report.layer_summary["application"].has_pending_review is False


@pytest.mark.asyncio
async def test_analyzer_respects_max_depth() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    # Chain: n0 → n1 → n2 → n3 → n4
    nodes = [_node(f"n{i}", "application") for i in range(5)]
    edges = [
        _edge(fr=f"n{i}", to=f"n{i+1}", rel="calls_api")
        for i in range(4)
    ]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, edges),
        ImpactAnalyzerConfig(max_depth=2),
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="n0"
    )
    assert report is not None
    distances = sorted({n.distance for n in report.downstream})
    assert distances == [1, 2]


@pytest.mark.asyncio
async def test_analyzer_truncates_when_exceeding_total_cap() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    nodes = [_node("root", "experience")] + [
        _node(f"c{i}", "application") for i in range(20)
    ]
    edges = [
        _edge(fr="root", to=f"c{i}", rel="calls_api") for i in range(20)
    ]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, edges),
        ImpactAnalyzerConfig(max_total_nodes=5),
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="root"
    )
    assert report is not None
    assert report.truncated is True
    assert len(report.downstream) <= 5


@pytest.mark.asyncio
async def test_analyzer_caps_at_max_per_layer() -> None:
    from app.actions import ImpactAnalyzer, ImpactAnalyzerConfig

    nodes = [_node("root", "experience")] + [
        _node(f"a{i}", "application") for i in range(10)
    ]
    edges = [
        _edge(fr="root", to=f"a{i}", rel="calls_api") for i in range(10)
    ]
    analyzer = ImpactAnalyzer(
        _FakeSessionFactory(nodes, edges),
        ImpactAnalyzerConfig(max_nodes_per_layer=3, max_total_nodes=100),
    )
    report = await analyzer.analyze(
        tenant_id="t1", product_id="p_lt5", root_atlas_node_id="root"
    )
    assert report is not None
    assert len(report.downstream) == 3
    assert report.truncated is True

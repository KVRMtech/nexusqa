"""Product Atlas — cross-layer projection of the Backbone graph.

Public surface:
    * ``Layer``                      — enum of layers (experience..compliance)
    * ``RelationKind``               — enum of cross-layer relationship types
    * ``AtlasNode``, ``AtlasEdge``,
      ``AlignmentProposal``          — DTOs
    * ``LayerClassifier``,
      ``HeuristicLayerClassifier``   — layer assignment
    * ``ProductResolver``            — auto-tag candidates against catalog
    * ``CrossModalAligner``          — emit edge/alignment proposals
    * ``AtlasBuilder``               — refresh projection after ingest
    * ``AtlasRepository``            — SQL access
"""

from __future__ import annotations

from .aligner import (
    AlignmentDecision,
    AlignmentResult,
    CrossModalAligner,
)
from .builder import AtlasBuilder, BuilderResult
from .layer_classifier import (
    HeuristicLayerClassifier,
    LayerClassifier,
    LayerVerdict,
)
from .models import (
    AtlasEdge,
    AtlasNode,
    AlignmentProposal,
    EdgeStatus,
    Layer,
    LayerStats,
    NodeCandidate,
    RelationKind,
)
from .product_resolver import ProductCatalogEntry, ProductResolver, ProductVerdict
from .repository import AtlasRepository, AtlasNodeConflict

__all__ = [
    "AlignmentDecision",
    "AlignmentProposal",
    "AlignmentResult",
    "AtlasBuilder",
    "AtlasEdge",
    "AtlasNode",
    "AtlasNodeConflict",
    "AtlasRepository",
    "BuilderResult",
    "CrossModalAligner",
    "EdgeStatus",
    "HeuristicLayerClassifier",
    "Layer",
    "LayerClassifier",
    "LayerStats",
    "LayerVerdict",
    "NodeCandidate",
    "ProductCatalogEntry",
    "ProductResolver",
    "ProductVerdict",
    "RelationKind",
]

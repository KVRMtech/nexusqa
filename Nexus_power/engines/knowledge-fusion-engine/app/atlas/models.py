"""DTOs and enums for the Atlas subsystem."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class Layer(str, Enum):
    """Atlas layers a node can belong to.

    Order matches the conceptual stack — experience on top, compliance
    is the cross-cutting jurisdictional layer that sits beside rules.
    """

    EXPERIENCE = "experience"     # UI / agent journeys
    APPLICATION = "application"   # API endpoints, services
    DATA = "data"                  # tables, schemas, lookups
    RULE = "rule"                  # business rules, policies
    TEST = "test"                  # test cases, scenarios
    OPS = "ops"                    # alerts, runbooks
    COMPLIANCE = "compliance"      # regulations, jurisdictions


class RelationKind(str, Enum):
    """Cross-layer relationship vocabulary."""

    CALLS_API = "calls_api"             # experience → application
    READS_TABLE = "reads_table"         # application → data
    WRITES_TABLE = "writes_table"       # application → data
    ENFORCES_RULE = "enforces_rule"     # application → rule
    TESTS_RULE = "tests_rule"           # test → rule
    NAVIGATES_TO = "navigates_to"       # experience → experience
    CONTAINS = "contains"               # experience → experience
    MONITORED_BY = "monitored_by"       # application → ops
    GOVERNED_BY = "governed_by"         # rule → compliance
    DEPENDS_ON = "depends_on"           # generic
    RELATED_TO = "related_to"           # fallback


class EdgeStatus(str, Enum):
    AUTO = "auto"
    CONFIRMED = "confirmed"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"


class AtlasNode(BaseModel):
    """One projected node in a product's atlas."""

    model_config = ConfigDict(extra="forbid")

    atlas_node_id: Optional[str] = None
    tenant_id: str
    product_id: str
    backbone_node_id: str
    node_type: str
    layer: Layer
    label: str = Field(min_length=1, max_length=512)
    source_session_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    source_segment_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    last_seen_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    version: int = 1


class AtlasEdge(BaseModel):
    """One cross-layer relationship."""

    model_config = ConfigDict(extra="forbid")

    edge_id: Optional[str] = None
    tenant_id: str
    product_id: str
    from_atlas_node_id: str
    to_atlas_node_id: str
    relation_type: RelationKind
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: EdgeStatus = EdgeStatus.AUTO
    evidence_json: dict[str, Any] = Field(default_factory=dict)
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AlignmentProposal(BaseModel):
    """A cross-modal alignment proposal awaiting review (or auto-applied)."""

    model_config = ConfigDict(extra="forbid")

    alignment_id: Optional[str] = None
    tenant_id: str
    product_id: str
    from_atlas_node_id: str
    to_atlas_node_id: str
    suggested_relation: RelationKind
    similarity: Optional[float] = None
    evidence_json: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None


class LayerStats(BaseModel):
    """Per-(product, layer) rollup used by the Atlas UI."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    product_id: str
    layer: Layer
    node_count: int = 0
    edge_count_in: int = 0
    edge_count_out: int = 0
    last_node_at: Optional[datetime] = None
    coverage_score: float = 0.0
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    updated_at: Optional[datetime] = None


class NodeCandidate(BaseModel):
    """Pre-projection input: what the builder needs to upsert a node.

    The builder receives a stream of these from ingest pipelines and
    folds them into the atlas. Fields not provided are filled by the
    classifier (layer) or product resolver (product_id).
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    backbone_node_id: str
    node_type: str
    label: str
    text: str = Field(default="", max_length=8000)
    product_id: Optional[str] = None
    product_candidates: list[str] = Field(default_factory=list)
    layer: Optional[Layer] = None
    layer_hint: Optional[str] = None
    source_session_id: Optional[str] = None
    source_artifact_id: Optional[str] = None
    source_segment_ids: list[str] = Field(default_factory=list)
    metadata_json: dict[str, Any] = Field(default_factory=dict)

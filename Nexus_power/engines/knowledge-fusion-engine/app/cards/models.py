"""DTOs and enums for the Knowledge Cards subsystem."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Enums ───────────────────────────────────────────────────────


class SourceStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DISSENTING = "dissenting"
    RETRACTED = "retracted"


class SourceType(str, Enum):
    SEGMENT = "segment"
    RULE = "rule"
    DOC = "doc"
    MANUAL = "manual"
    EXTERNAL = "external"


# ── Card + Source ───────────────────────────────────────────────


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,255}$")


class CardSource(BaseModel):
    """One contributing source on a card."""

    model_config = ConfigDict(extra="forbid")

    id: Optional[int] = None
    card_id: str
    tenant_id: str
    source_type: SourceType
    source_id: str
    backbone_node_id: Optional[str] = None
    session_id: Optional[str] = None
    artifact_id: Optional[str] = None
    sme_id: Optional[str] = None
    sme_role: Optional[str] = None
    stated_at: Optional[date] = None
    similarity_to_canonical: Optional[float] = None
    weight: float = 1.0
    status: SourceStatus = SourceStatus.ACTIVE
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Card(BaseModel):
    """One knowledge card."""

    model_config = ConfigDict(extra="forbid")

    card_id: str
    tenant_id: str
    topic_slug: str
    topic_label: str
    canonical_statement: str
    canonical_confidence: float = 0.0
    consensus_score: float = 0.0
    lifecycle_state: str = "tribal"
    authority_chain: list[dict[str, Any]] = Field(default_factory=list)
    contributing_count: int = 0
    dissent_count: int = 0
    product_id: Optional[str] = None
    validity_start: Optional[date] = None
    validity_end: Optional[date] = None
    jurisdiction: Optional[str] = None
    superseded_by: Optional[str] = None
    halflife_days: int = 270
    last_verified_at: Optional[datetime] = None
    verify_due_at: Optional[datetime] = None
    backbone_node_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("topic_slug")
    @classmethod
    def _slug_pattern(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                f"topic_slug must match {_SLUG_RE.pattern}"
            )
        return v


# ── Candidates (inputs to the synthesizer) ─────────────────────


class SourceCandidate(BaseModel):
    """One transcript segment / rule pending card synthesis.

    The synthesizer accepts a list of these and decides how to fold
    each into the card graph.
    """

    model_config = ConfigDict(extra="forbid")

    source_type: SourceType = SourceType.SEGMENT
    source_id: str
    backbone_node_id: Optional[str] = None
    text: str
    session_id: Optional[str] = None
    artifact_id: Optional[str] = None
    sme_id: Optional[str] = None
    sme_role: Optional[str] = None
    stated_at: Optional[date] = None
    product_id: Optional[str] = None
    jurisdiction: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class NewCardCandidate(BaseModel):
    """Material used to bootstrap a brand-new card."""

    model_config = ConfigDict(extra="forbid")

    topic_slug: str
    topic_label: str
    canonical_statement: str
    product_id: Optional[str] = None
    jurisdiction: Optional[str] = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("topic_slug")
    @classmethod
    def _slug_pattern(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                f"topic_slug must match {_SLUG_RE.pattern}"
            )
        return v

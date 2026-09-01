"""DTOs + enums for the action layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ActionKind(str, Enum):
    SANDBOX_RUN = "sandbox_run"
    GITHUB_PR = "github_pr"
    CONFLUENCE_PUBLISH = "confluence_publish"
    IMPACT_ANALYSIS = "impact_analysis"
    TOUR_GENERATE = "tour_generate"


class ActionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TourStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ActionInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: str
    tenant_id: str
    kind: ActionKind
    trigger_dispatch_id: Optional[str] = None
    trigger_user_id: Optional[str] = None
    trace_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    status: ActionStatus = ActionStatus.QUEUED
    error: Optional[str] = None
    latency_ms: Optional[int] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TourSegment(BaseModel):
    """One step in a synthesized tour playlist."""

    model_config = ConfigDict(extra="forbid")

    atlas_node_id: str
    label: str
    layer: str
    segment_ids: list[str] = Field(default_factory=list)
    speaker_id: Optional[str] = None
    estimated_seconds: int = 30
    ordinal: int = 0


class SynthesizedTour(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tour_id: Optional[str] = None
    tenant_id: str
    product_id: str
    title: str = Field(min_length=1, max_length=256)
    persona: Optional[str] = Field(default=None, max_length=64)
    target_minutes: Optional[int] = Field(default=None, ge=1, le=240)
    playlist: list[TourSegment] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    atlas_node_ids: list[str] = Field(default_factory=list)
    status: TourStatus = TourStatus.DRAFT
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ImpactAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str
    tenant_id: str
    product_id: str
    root_atlas_node_id: str
    change_description: Optional[str] = None
    downstream: list[dict[str, Any]] = Field(default_factory=list)
    upstream: list[dict[str, Any]] = Field(default_factory=list)
    layer_summary: dict[str, Any] = Field(default_factory=dict)
    estimated_blast_radius: int = 0
    requested_by: Optional[str] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

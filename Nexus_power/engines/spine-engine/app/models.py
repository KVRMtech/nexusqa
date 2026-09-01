"""
Spine Engine — Shared Pydantic models.

Models used by both parsers and the engine routes:
DocumentChunk and ExtractedTable.
"""

from __future__ import annotations

import uuid
from typing import Optional, Any

from pydantic import BaseModel, Field
from nexus_sdk.models import SourceReference, Confidence


class DocumentChunk(BaseModel):
    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    chunk_type: str  # ChunkType enum value
    content: str
    page_number: Optional[int] = None
    section: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    char_count: int = 0
    source: SourceReference = Field(default_factory=SourceReference)


class ExtractedTable(BaseModel):
    table_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    page_number: Optional[int] = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    row_count: int = 0
    col_count: int = 0
    table_type: Optional[str] = None  # rate_table, comparison, schedule, etc.
    metadata: dict[str, Any] = Field(default_factory=dict)

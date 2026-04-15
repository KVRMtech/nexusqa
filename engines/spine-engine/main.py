"""
Nexus Spine Engine — Document Ingestion & Knowledge Extraction (v0.2.0 Modular).

The spine that reads and structures all written knowledge.
60% of insurance knowledge lives in DOCUMENTS, not conversations.

v0.2.0 — Refactored: parsers and processor extracted to app/ sub-package.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any
from enum import Enum

from fastapi import Depends, HTTPException, BackgroundTasks, UploadFile, File, Form
from pydantic import BaseModel, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import (
    NexusRequest, NexusResponse, JobResponse, JobStatus,
    SourceReference, Confidence,
)
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent

# ─── Imports from modular app/ sub-package ─────────────────────

from app.models import DocumentChunk, ExtractedTable
from app.processor import DocumentProcessor
from app.parsers import pdf as pdf_mod, word as word_mod, powerpoint as pptx_mod
from app.parsers.csv_parser import CSVParser
from app.parsers.text import TextParser


# ─── Configuration ─────────────────────────────────────────────

class SpineConfig(EngineConfig):
    engine_name: str = "spine"
    engine_port: int = 8009

    # Document storage
    document_storage_path: str = "/data/nexus/documents"

    # Chunking
    chunk_size: int = 1000
    chunk_overlap: int = 200
    max_document_size_mb: int = 100

    # Table extraction
    table_extraction_enabled: bool = True

    # OCR for scanned PDFs
    ocr_enabled: bool = True
    ocr_language: str = "eng"

    # Supported formats
    supported_formats: str = "pdf,xlsx,xls,docx,doc,pptx,ppt,csv,txt,md"


# ─── Enums ────────────────────────────────────────────────────

class DocumentType(str, Enum):
    """Insurance document classifications."""
    RATE_FILING = "rate_filing"
    RATE_TABLE = "rate_table"
    BRD = "business_requirements_document"
    TRAINING_DECK = "training_deck"
    COMPLIANCE_MANUAL = "compliance_manual"
    UNDERWRITING_GUIDE = "underwriting_guide"
    PROCEDURE_DOCUMENT = "procedure_document"
    POLICY_FORM = "policy_form"
    APPLICATION_FORM = "application_form"
    CLAIM_FORM = "claim_form"
    ACTUARIAL_MEMO = "actuarial_memo"
    STATE_APPROVAL = "state_approval"
    GENERAL = "general"
    UNKNOWN = "unknown"


class ChunkType(str, Enum):
    TEXT = "text"
    TABLE = "table"
    HEADING = "heading"
    LIST = "list"
    IMAGE_CAPTION = "image_caption"
    METADATA = "metadata"


class ParseStatus(str, Enum):
    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    CLASSIFYING = "classifying"
    STORING = "storing"
    COMPLETED = "completed"
    FAILED = "failed"


# ─── Document Classification Keywords ─────────────────────────

CLASSIFICATION_KEYWORDS: dict[DocumentType, list[str]] = {
    DocumentType.RATE_FILING: [
        "rate filing", "premium rate", "actuarial", "loss ratio",
        "rate schedule", "filed rate", "rate approval", "serff",
        "rate justification", "experience data",
    ],
    DocumentType.RATE_TABLE: [
        "rate table", "premium table", "age band", "monthly premium",
        "annual premium", "per 1000", "per thousand", "rate per",
        "smoker rate", "non-smoker rate", "preferred plus",
    ],
    DocumentType.BRD: [
        "business requirement", "functional requirement", "use case",
        "acceptance criteria", "user story", "business rule",
        "system requirement", "change request", "scope",
    ],
    DocumentType.TRAINING_DECK: [
        "training", "onboarding", "knowledge transfer", "overview",
        "agenda", "objectives", "key takeaways", "demo",
        "walkthrough", "how to",
    ],
    DocumentType.COMPLIANCE_MANUAL: [
        "compliance", "regulatory", "naic", "state requirement",
        "filing requirement", "suitability", "market conduct",
        "anti-money laundering", "aml", "kyc",
    ],
    DocumentType.UNDERWRITING_GUIDE: [
        "underwriting", "risk classification", "medical history",
        "build chart", "preferred criteria", "declination",
        "substandard", "table rating", "flat extra",
    ],
    DocumentType.PROCEDURE_DOCUMENT: [
        "procedure", "step by step", "workflow", "process flow",
        "standard operating procedure", "sop", "guideline",
    ],
    DocumentType.POLICY_FORM: [
        "policy form", "certificate", "endorsement", "rider form",
        "schedule page", "declarations", "insuring agreement",
    ],
    DocumentType.APPLICATION_FORM: [
        "application", "applicant information", "proposed insured",
        "beneficiary designation", "medical questions",
    ],
    DocumentType.CLAIM_FORM: [
        "claim form", "proof of loss", "claim submission",
        "claimant", "date of loss", "cause of loss",
    ],
    DocumentType.ACTUARIAL_MEMO: [
        "actuarial memorandum", "pricing basis", "mortality table",
        "cso table", "reserve basis", "cash value", "nonforfeiture",
    ],
    DocumentType.STATE_APPROVAL: [
        "approved", "effective date", "state filing", "department of insurance",
        "commissioner", "approval letter", "objection letter",
    ],
}


# ─── Request / Response Models ─────────────────────────────────

class IngestDocumentResponse(NexusResponse):
    document_id: str
    job_id: str
    filename: str
    file_size_bytes: int
    detected_type: DocumentType = DocumentType.UNKNOWN
    status: ParseStatus = ParseStatus.PENDING


class DocumentStatusResponse(NexusResponse):
    document_id: str
    filename: str
    status: ParseStatus
    detected_type: DocumentType
    page_count: int = 0
    chunk_count: int = 0
    table_count: int = 0
    processing_time_ms: float = 0
    error: Optional[str] = None


class GetChunksRequest(NexusRequest):
    document_id: str
    chunk_types: Optional[list[ChunkType]] = None
    page_numbers: Optional[list[int]] = None
    search_text: Optional[str] = None


class GetChunksResponse(NexusResponse):
    chunks: list[DocumentChunk] = Field(default_factory=list)
    total_chunks: int = 0


class GetTablesResponse(NexusResponse):
    tables: list[ExtractedTable] = Field(default_factory=list)
    total_tables: int = 0


class SearchDocumentsRequest(NexusRequest):
    query: str = Field(..., description="Search query")
    document_ids: Optional[list[str]] = None
    document_types: Optional[list[DocumentType]] = None
    max_results: int = Field(default=20, le=100)


class SearchDocumentsResponse(NexusResponse):
    results: list[dict] = Field(default_factory=list)
    total_results: int = 0


# ─── The Spine Engine ──────────────────────────────────────────

class SpineEngine(NexusEngine):

    def __init__(self):
        super().__init__(
            name="spine",
            version="0.2.0",
            config=SpineConfig(engine_name="spine", engine_port=8009),
            description="Document ingestion and knowledge extraction for insurance QA",
        )
        self.processor: Optional[DocumentProcessor] = None
        self.documents: dict[str, dict] = {}
        self.document_chunks: dict[str, list[DocumentChunk]] = {}
        self.document_tables: dict[str, list[ExtractedTable]] = {}

    async def on_startup(self):
        """Initialize document processor, database pool, and inject event bus."""
        # Inject event bus into parser modules that have stub fallbacks
        for mod in (pdf_mod, word_mod, pptx_mod):
            mod.set_event_bus(self.event_bus)

        self.processor = DocumentProcessor(
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
        )

        # Initialize PostgreSQL connection pool for canonical artifact persistence
        try:
            from nexus_sdk.db import Database
            from nexus_sdk.config import PostgresConfig
            pg_config = PostgresConfig()
            self._database = Database(pg_config)
            await self._database.connect()
            self.db_pool = self._database.session
            self.health.set_mode("database", "postgresql")
        except Exception as e:
            import logging
            logging.getLogger("spine").warning(f"spine.db_init_failed: {e}")
            self._database = None
            self.db_pool = None
            self.health.set_mode("database", "unavailable")

        # Load document type classification keywords from domain plugins
        try:
            doc_type_ext = self.plugin_registry.get_merged_document_types()
            if doc_type_ext and doc_type_ext.document_types:
                for dt in doc_type_ext.document_types:
                    doc_type_key = dt.type_id
                    if dt.classification_keywords:
                        CLASSIFICATION_KEYWORDS.setdefault(doc_type_key, []).extend(
                            dt.classification_keywords
                        )
        except Exception:
            pass

        # Ensure storage directory exists
        storage_path = self.config.document_storage_path
        os.makedirs(storage_path, exist_ok=True)
        self.health.set_mode("document_processor", "local")
        self.health.set_mode("document_storage", "filesystem")

    async def on_shutdown(self):
        """Gracefully close the PostgreSQL connection pool."""
        if getattr(self, "_database", None):
            await self._database.disconnect()

    def register_routes(self, app):

        engine = self

        # ── Upload & Ingest Document ───────────────────────────

        @app.post(
            "/api/v1/spine/ingest",
            response_model=IngestDocumentResponse,
        )
        async def ingest_document(
            background_tasks: BackgroundTasks,
            file: UploadFile = File(...),
            tenant_id: str = Form(...),
            session_id: Optional[str] = Form(default=None),
            user: NexusUser = Depends(get_current_user),
        ):
            """Upload and ingest a document."""
            content = await file.read()
            filename = file.filename or "unknown"

            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            supported = engine.config.supported_formats.split(",")
            if ext not in supported:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported format '{ext}'. Supported: {supported}",
                )

            max_bytes = engine.config.max_document_size_mb * 1024 * 1024
            if len(content) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max: {engine.config.max_document_size_mb}MB",
                )

            document_id = str(uuid.uuid4())
            job_id = str(uuid.uuid4())

            job = JobResponse(
                job_id=job_id,
                status=JobStatus.PENDING,
                trace_id=str(uuid.uuid4()),
                engine="spine",
            )
            await engine.job_store.set_job(job_id, job.model_dump())

            engine.documents[document_id] = {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "filename": filename,
                "file_size_bytes": len(content),
                "uploaded_by": user.user_id,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "status": ParseStatus.PENDING.value,
                "detected_type": DocumentType.UNKNOWN.value,
                "job_id": job_id,
            }

            background_tasks.add_task(
                _process_document,
                engine=engine,
                content=content,
                filename=filename,
                document_id=document_id,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )

            return IngestDocumentResponse(
                success=True,
                trace_id=job.trace_id,
                engine="spine",
                engine_version="0.2.0",
                document_id=document_id,
                job_id=job_id,
                filename=filename,
                file_size_bytes=len(content),
            )

        # ── Get Document Status ────────────────────────────────

        @app.get(
            "/api/v1/spine/documents/{document_id}",
            response_model=DocumentStatusResponse,
        )
        async def get_document_status(
            document_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Get the status and metadata of an ingested document."""
            doc = engine.documents.get(document_id)
            if not doc:
                raise HTTPException(status_code=404, detail="Document not found")

            chunks = engine.document_chunks.get(document_id, [])
            tables = engine.document_tables.get(document_id, [])

            return DocumentStatusResponse(
                success=True,
                trace_id=str(uuid.uuid4()),
                engine="spine",
                engine_version="0.2.0",
                document_id=document_id,
                filename=doc["filename"],
                status=ParseStatus(doc.get("status", "pending")),
                detected_type=DocumentType(doc.get("detected_type", "unknown")),
                page_count=doc.get("page_count", 0),
                chunk_count=len(chunks),
                table_count=len(tables),
                processing_time_ms=doc.get("processing_time_ms", 0),
                error=doc.get("error"),
            )

        # ── Get Document Chunks ────────────────────────────────

        @app.post(
            "/api/v1/spine/documents/{document_id}/chunks",
            response_model=GetChunksResponse,
        )
        async def get_chunks(
            document_id: str,
            req: GetChunksRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Retrieve text chunks from a parsed document."""
            if document_id not in engine.documents:
                raise HTTPException(status_code=404, detail="Document not found")

            chunks = engine.document_chunks.get(document_id, [])

            if req.chunk_types:
                chunks = [c for c in chunks if c.chunk_type in [ct.value for ct in req.chunk_types]]
            if req.page_numbers:
                chunks = [c for c in chunks if c.page_number in req.page_numbers]
            if req.search_text:
                search_lower = req.search_text.lower()
                chunks = [c for c in chunks if search_lower in c.content.lower()]

            return GetChunksResponse(
                success=True,
                trace_id=req.trace_id,
                engine="spine",
                engine_version="0.2.0",
                chunks=chunks,
                total_chunks=len(chunks),
            )

        # ── Get Tables ─────────────────────────────────────────

        @app.get(
            "/api/v1/spine/documents/{document_id}/tables",
            response_model=GetTablesResponse,
        )
        async def get_tables(
            document_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Retrieve extracted tables from a document."""
            if document_id not in engine.documents:
                raise HTTPException(status_code=404, detail="Document not found")

            tables = engine.document_tables.get(document_id, [])

            return GetTablesResponse(
                success=True,
                trace_id=str(uuid.uuid4()),
                engine="spine",
                engine_version="0.2.0",
                tables=tables,
                total_tables=len(tables),
            )

        # ── List Documents ─────────────────────────────────────

        @app.get("/api/v1/spine/documents")
        async def list_documents(
            tenant_id: Optional[str] = None,
            session_id: Optional[str] = None,
            document_type: Optional[DocumentType] = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """List all ingested documents, optionally filtered."""
            docs = list(engine.documents.values())

            if tenant_id:
                docs = [d for d in docs if d.get("tenant_id") == tenant_id]
            if session_id:
                docs = [d for d in docs if d.get("session_id") == session_id]
            if document_type:
                docs = [d for d in docs if d.get("detected_type") == document_type.value]

            return {"documents": docs, "total": len(docs)}

        # ── Search Across Documents ────────────────────────────

        @app.post(
            "/api/v1/spine/search",
            response_model=SearchDocumentsResponse,
        )
        async def search_documents(
            req: SearchDocumentsRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Search across all ingested documents (keyword matching)."""
            query_lower = req.query.lower()
            results = []

            for doc_id, chunks in engine.document_chunks.items():
                doc_meta = engine.documents.get(doc_id, {})

                if req.document_ids and doc_id not in req.document_ids:
                    continue
                if req.document_types:
                    doc_type = doc_meta.get("detected_type", "unknown")
                    if doc_type not in [dt.value for dt in req.document_types]:
                        continue

                for chunk in chunks:
                    if query_lower in chunk.content.lower():
                        count = chunk.content.lower().count(query_lower)
                        results.append({
                            "document_id": doc_id,
                            "chunk_id": chunk.chunk_id,
                            "filename": doc_meta.get("filename", ""),
                            "document_type": doc_meta.get("detected_type", "unknown"),
                            "page_number": chunk.page_number,
                            "section": chunk.section,
                            "content_snippet": chunk.content[:300],
                            "relevance_score": count,
                        })

            results.sort(key=lambda r: r["relevance_score"], reverse=True)
            results = results[:req.max_results]

            return SearchDocumentsResponse(
                success=True,
                trace_id=req.trace_id,
                engine="spine",
                engine_version="0.2.0",
                results=results,
                total_results=len(results),
            )

        # ── URL-Based Ingestion ────────────────────────────────

        class IngestURLRequest(NexusRequest):
            """Ingest a document or media from a remote URL."""
            url: str = Field(..., description="URL to fetch media/document from")
            tenant_id: str
            session_id: Optional[str] = None
            source_type: str = Field("url", description="Ingestion source type")

        @app.post("/api/v1/spine/ingest-url")
        async def ingest_url(
            req: IngestURLRequest,
            background_tasks: BackgroundTasks,
            user: NexusUser = Depends(get_current_user),
        ):
            """Ingest a document or media from a URL.

            Downloads the resource, validates format/size, then routes
            to the standard document processing pipeline.
            """
            import httpx as _httpx
            from urllib.parse import urlparse

            parsed = urlparse(req.url)
            if parsed.scheme not in ("http", "https"):
                raise HTTPException(status_code=400, detail="Only http/https URLs are supported")

            # Restrict to non-private networks (basic SSRF prevention)
            hostname = parsed.hostname or ""
            if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
                raise HTTPException(status_code=400, detail="Local URLs are not permitted")

            # Extract filename from URL path
            path_parts = parsed.path.rstrip("/").rsplit("/", 1)
            filename = path_parts[-1] if len(path_parts) > 1 and path_parts[-1] else "download"

            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            supported = engine.config.supported_formats.split(",")
            # Also allow media extensions that canonical pipeline handles
            media_exts = {"wav", "mp3", "mp4", "avi", "mov", "mkv", "webm", "m4a", "flac", "ogg"}
            all_allowed = set(supported) | media_exts
            if ext and ext not in all_allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported format '{ext}'. Supported: {sorted(all_allowed)}",
                )

            max_bytes = engine.config.max_document_size_mb * 1024 * 1024

            # Download with size limit and timeout
            try:
                async with _httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                    resp = await http.get(req.url)
                    resp.raise_for_status()
                    content = resp.content
            except _httpx.HTTPStatusError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"URL returned HTTP {exc.response.status_code}",
                )
            except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Cannot fetch URL: {type(exc).__name__}",
                )

            if len(content) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Downloaded content too large ({len(content)} bytes). Max: {engine.config.max_document_size_mb}MB",
                )

            if len(content) == 0:
                raise HTTPException(status_code=422, detail="URL returned empty content")

            document_id = str(uuid.uuid4())
            job_id = str(uuid.uuid4())

            job = JobResponse(
                job_id=job_id,
                status=JobStatus.PENDING,
                trace_id=str(uuid.uuid4()),
                engine="spine",
            )
            await engine.job_store.set_job(job_id, job.model_dump())

            engine.documents[document_id] = {
                "document_id": document_id,
                "tenant_id": req.tenant_id,
                "session_id": req.session_id,
                "filename": filename,
                "file_size_bytes": len(content),
                "source_url": req.url,
                "source_type": "url",
                "uploaded_by": user.user_id,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "status": ParseStatus.PENDING.value,
                "detected_type": DocumentType.UNKNOWN.value,
                "job_id": job_id,
            }

            background_tasks.add_task(
                _process_document,
                engine=engine,
                content=content,
                filename=filename,
                document_id=document_id,
                tenant_id=req.tenant_id,
                session_id=req.session_id,
                job_id=job_id,
            )

            return {
                "success": True,
                "document_id": document_id,
                "job_id": job_id,
                "filename": filename,
                "file_size_bytes": len(content),
                "source_url": req.url,
                "source_type": "url",
            }

        # ── Job Status ─────────────────────────────────────────

        @app.get("/api/v1/spine/jobs/{job_id}")
        async def get_job(
            job_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Check status of a document ingestion job."""
            job = await engine.job_store.get_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            return job

        # ── Stats ──────────────────────────────────────────────

        @app.get("/api/v1/spine/stats")
        async def get_stats(user: NexusUser = Depends(get_current_user)):
            """Engine statistics."""
            total_chunks = sum(len(c) for c in engine.document_chunks.values())
            total_tables = sum(len(t) for t in engine.document_tables.values())
            type_counts: dict[str, int] = {}
            for doc in engine.documents.values():
                dt = doc.get("detected_type", "unknown")
                type_counts[dt] = type_counts.get(dt, 0) + 1

            return {
                "engine": "spine",
                "version": "0.2.0",
                "total_documents": len(engine.documents),
                "total_chunks": total_chunks,
                "total_tables": total_tables,
                "document_types": type_counts,
                "supported_formats": engine.config.supported_formats.split(","),
                "capabilities": [
                    "pdf_parsing",
                    "excel_parsing",
                    "word_parsing",
                    "powerpoint_parsing",
                    "csv_parsing",
                    "document_classification",
                    "text_chunking",
                    "table_extraction",
                    "keyword_search",
                    "media_probe",
                    "visual_graph",
                    "canonical_artifact_persistence",
                    "url_ingestion",
                ],
            }

        # ════════════════════════════════════════════════════════
        #  CANONICAL PROCESSING ENDPOINTS (Phase 2)
        # ════════════════════════════════════════════════════════

        # ── Probe Media ────────────────────────────────────────

        @app.post("/api/v1/spine/probe-media")
        async def probe_media(
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Probe uploaded media files for duration, codecs, resolution.

            Accepts shared-filesystem paths — runs ffprobe directly,
            zero file copying, zero memory pressure (even for multi-GB videos).
            """
            if request is None:
                request = {}
            tenant_id = request.get("tenant_id", "")
            session_id = request.get("session_id", "")
            audio_file_path = request.get("audio_file_path")
            video_file_path = request.get("video_file_path")

            result: dict[str, Any] = {
                "success": True,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "duration_seconds": 0.0,
                "audio": None,
                "video": None,
            }

            if audio_file_path and os.path.exists(audio_file_path):
                audio_meta = await asyncio.to_thread(
                    _ffprobe_file, audio_file_path
                )
                result["audio"] = audio_meta
                result["duration_seconds"] = max(
                    result["duration_seconds"],
                    audio_meta.get("duration_seconds", 0.0),
                )

            if video_file_path and os.path.exists(video_file_path):
                video_meta = await asyncio.to_thread(
                    _ffprobe_file, video_file_path
                )
                result["video"] = video_meta
                result["duration_seconds"] = max(
                    result["duration_seconds"],
                    video_meta.get("duration_seconds", 0.0),
                )

            return result

        # ── Build Visual Graph ─────────────────────────────────

        @app.post("/api/v1/spine/build-visual-graph")
        async def build_visual_graph(
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Build a screen-flow graph from keyframes, OCR, and scene transitions.

            Constructs a directed graph where nodes are unique screens
            and edges represent transitions observed in the video.
            """
            if request is None:
                request = {}
            frames = request.get("frames", [])
            app_types_seen = request.get("application_types_seen", [])
            total_extracted = request.get("total_frames_extracted", 0)

            if not frames:
                return {
                    "success": True,
                    "graph": {"nodes": [], "edges": []},
                    "screen_count": 0,
                }

            nodes: list[dict] = []
            edges: list[dict] = []
            seen_titles: dict[str, int] = {}  # title -> node index
            prev_node_idx: Optional[int] = None

            for frame in frames:
                is_keyframe = frame.get("is_keyframe", False)
                if not is_keyframe:
                    continue

                title = frame.get("page_title", "") or frame.get("description", "")[:80]
                app_type = frame.get("application_type", "unknown")
                timestamp = frame.get("timestamp_seconds", 0.0)

                if title in seen_titles:
                    node_idx = seen_titles[title]
                else:
                    node_idx = len(nodes)
                    seen_titles[title] = node_idx
                    nodes.append({
                        "index": node_idx,
                        "title": title,
                        "application_type": app_type,
                        "first_seen_at": timestamp,
                        "frame_count": 0,
                        "ocr_sample": (
                            frame.get("extracted_text", "")[:200]
                        ),
                    })

                nodes[node_idx]["frame_count"] += 1

                # Create edge from previous screen to this one
                if prev_node_idx is not None and prev_node_idx != node_idx:
                    edge_key = f"{prev_node_idx}->{node_idx}"
                    existing = next(
                        (e for e in edges if e.get("_key") == edge_key), None
                    )
                    if existing:
                        existing["transition_count"] += 1
                    else:
                        edges.append({
                            "_key": edge_key,
                            "from_node": prev_node_idx,
                            "to_node": node_idx,
                            "transition_count": 1,
                            "first_transition_at": timestamp,
                        })

                prev_node_idx = node_idx

            # Clean up internal keys
            for edge in edges:
                edge.pop("_key", None)

            return {
                "success": True,
                "graph": {
                    "nodes": nodes,
                    "edges": edges,
                },
                "screen_count": len(nodes),
                "transition_count": len(edges),
                "application_types_seen": app_types_seen,
                "total_keyframes_processed": sum(
                    n["frame_count"] for n in nodes
                ),
            }

        # ── Persist Canonical Artifact ─────────────────────────

        @app.post("/api/v1/spine/persist-canonical-artifact")
        async def persist_canonical_artifact(
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Merge all pipeline outputs into a canonical artifact and persist.

            Checks media_fingerprint for re-upload deduplication.
            Stores in PostgreSQL via the SDK's DB layer.

            Phase 1.4: Artifact status is the official completion signal.
            The artifact enters 'processing' on creation and moves to
            'completed' when persistence succeeds.  The quality gate
            stage later updates quality fields via the update endpoint.
            """
            if request is None:
                request = {}

            tenant_id = request.get("tenant_id", "")
            session_id = request.get("session_id", "")
            fingerprint = request.get("media_fingerprint", "")

            # Fingerprint dedup check via Redis cache
            if fingerprint:
                cache_key = f"canonical:artifact:{tenant_id}:{fingerprint}"
                cached = await _redis_get(engine, cache_key)
                if cached:
                    return {
                        "success": True,
                        "artifact_id": cached.get("artifact_id", ""),
                        "status": "cached",
                        "message": "Existing artifact found for this media fingerprint",
                    }

            # If no session_id, this is a probe-only request (cache check).
            # Do NOT create an artifact — just report cache miss.
            if not session_id:
                return {
                    "success": True,
                    "artifact_id": "",
                    "status": "no_match",
                    "message": "No cached artifact for this fingerprint",
                }

            # Build artifact — use pre-allocated artifact_id if provided (from orchestrator
            # upload endpoint), otherwise generate a new one for backwards compatibility.
            artifact_id = request.get("artifact_id") or str(uuid.uuid4())
            now = datetime.now(timezone.utc)

            # Extract data from upstream stages
            media_probe = request.get("media_probe") or {}
            safe_transcript = request.get("safe_transcript") or {}
            raw_transcript = request.get("raw_transcript") or {}
            visual_analysis = request.get("visual_analysis") or {}
            visual_graph = request.get("visual_graph") or {}

            # Provenance fields (Phase 1.1 / 1.5)
            workflow_id = request.get("workflow_id", "")
            source_type = request.get("source_type", "")
            source_filename = request.get("source_filename", "")
            created_by = request.get("created_by", "")

            raw_text = raw_transcript.get("transcript_text") or " ".join(
                seg.get("text", "").strip()
                for seg in raw_transcript.get("segments", [])
                if seg.get("text", "").strip()
            )

            # Shield.redact returns `safe_text`, not `redacted_text`
            safe_text = (
                safe_transcript.get("safe_text")
                or safe_transcript.get("redacted_text", "")
            )
            if not safe_text:
                safe_text = raw_text

            visual_summary = ""
            frames = visual_analysis.get("frames", [])
            if frames:
                descriptions = [
                    f.get("description", "") for f in frames if f.get("is_keyframe")
                ]
                visual_summary = " → ".join(d for d in descriptions if d)[:2000]

            artifact_data = {
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "media_fingerprint": fingerprint,
                "status": "persisted",  # Phase 1.4: data stored; quality gate promotes to completed (trusted)
                "workflow_id": workflow_id,
                "source_type": source_type,
                "source_filename": source_filename,
                "created_by": created_by,
                "duration_seconds": media_probe.get("duration_seconds", 0.0),
                "scene_count": visual_analysis.get("total_frames_analyzed", 0),
                "frame_count": visual_analysis.get("total_frames_extracted", 0),
                "safe_transcript_text": safe_text,
                "raw_transcript_text": raw_text,
                "visual_summary": visual_summary,
                "application_types_seen": visual_analysis.get(
                    "application_types_seen", []
                ),
                "brain_quality_score": None,
                "quality_gate_passed": False,
                "quality_gate_outcome": None,
                "full_artifact_json": {
                    "media_probe": media_probe,
                    "transcript": raw_transcript,
                    "safe_transcript": safe_transcript,
                    "visual_analysis": visual_analysis,
                    "visual_graph": visual_graph,
                    "model_provenance": {
                        "ears_model": raw_transcript.get("model_version", ""),
                        "eyes_model": visual_analysis.get("model_version", ""),
                        "processing_profile": request.get("processing_profile", ""),
                    },
                },
                "processing_time_seconds": visual_analysis.get(
                    "processing_time_seconds", 0.0
                ),
                "created_at": now.isoformat(),
                "completed_at": None,  # Set by quality gate or status endpoint — not at persist time
            }

            # Persist to PostgreSQL
            persisted = await _persist_artifact_to_db(engine, artifact_data)

            # Cache in Redis for fast lookup
            if fingerprint:
                cache_key = f"canonical:artifact:{tenant_id}:{fingerprint}"
                await _redis_set(
                    engine, cache_key, {"artifact_id": artifact_id}
                )

            # Also cache by session
            session_cache_key = f"canonical:artifact:{tenant_id}:{session_id}"
            await _redis_set(
                engine, session_cache_key, {"artifact_id": artifact_id}
            )

            # Emit event
            if engine.event_bus:
                try:
                    await engine.event_bus.publish(NexusEvent(
                        event_type="spine.canonical_artifact.ready",
                        source="spine",
                        data={
                            "tenant_id": tenant_id,
                            "session_id": session_id,
                            "artifact_id": artifact_id,
                            "media_fingerprint": fingerprint,
                            "frame_count": artifact_data["frame_count"],
                            "has_transcript": bool(safe_text),
                        },
                    ))
                except Exception:
                    pass

            return {
                "success": True,
                "artifact_id": artifact_id,
                "status": "completed" if persisted else "cached_only",
                "session_id": session_id,
                "workflow_id": workflow_id,
            }

        # ── Update Artifact Quality Gate ──────────────────────

        @app.post("/api/v1/spine/artifacts/{artifact_id}/quality-gate")
        async def update_artifact_quality_gate(
            artifact_id: str,
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Update canonical artifact with quality gate results.

            Phase 1.4: Artifact status is the official completion signal.
            After Brain quality gate evaluates, this endpoint writes back:
            - brain_quality_score
            - quality_gate_passed
            - quality_gate_outcome (pass / fail / needs_review)
            - status update (completed stays, or moves to needs_review)
            """
            if request is None:
                request = {}

            score = request.get("brain_quality_score")
            passed = request.get("quality_gate_passed", False)
            outcome = request.get("quality_gate_outcome", "pass" if passed else "fail")
            error_msg = request.get("error")
            review_reasons = request.get("review_reasons", [])

            try:
                if not hasattr(engine, "db_pool") or not engine.db_pool:
                    return {"success": False, "error": "database not available"}

                from nexus_sdk.db.models import CanonicalArtifactRow
                from sqlalchemy import select, update as sa_update

                async with engine.db_pool() as session:
                    # Determine new status based on outcome
                    # A failed quality gate must never leave the artifact as 'completed'
                    new_status = None
                    if outcome == "pass":
                        new_status = "completed"
                    elif outcome == "needs_review" or outcome == "fail":
                        new_status = "needs_review"
                    if error_msg:
                        new_status = "failed"

                    update_vals = {
                        "brain_quality_score": score,
                        "quality_gate_passed": passed,
                        "quality_gate_outcome": outcome,
                    }
                    if new_status:
                        update_vals["status"] = new_status
                    if error_msg:
                        update_vals["error"] = error_msg

                    # Merge review_reasons into full_artifact_json
                    if review_reasons:
                        row = await session.execute(
                            select(CanonicalArtifactRow).where(
                                CanonicalArtifactRow.artifact_id == artifact_id
                            )
                        )
                        art = row.scalar_one_or_none()
                        if art and art.full_artifact_json:
                            merged = dict(art.full_artifact_json)
                        else:
                            merged = {}
                        merged["review_reasons"] = review_reasons
                        update_vals["full_artifact_json"] = merged

                    stmt = (
                        sa_update(CanonicalArtifactRow)
                        .where(CanonicalArtifactRow.artifact_id == artifact_id)
                        .values(**update_vals)
                    )
                    result = await session.execute(stmt)
                    await session.commit()

                    # NOTE: Do NOT invalidate the fingerprint→artifact_id Redis cache here.
                    # Quality-gate updates add metadata but don't change the
                    # artifact identity.  Deleting the cache entry would break
                    # duplicate-upload detection (the entry was just written by
                    # the artifact_persistence stage moments earlier).

                    return {
                        "success": True,
                        "artifact_id": artifact_id,
                        "quality_gate_outcome": outcome,
                        "quality_gate_passed": passed,
                        "brain_quality_score": score,
                    }
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        # ── Update Artifact Status ─────────────────────────────

        @app.post("/api/v1/spine/artifacts/{artifact_id}/status")
        async def update_artifact_status(
            artifact_id: str,
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Update canonical artifact status (Phase 1.4).

            Used by the orchestrator to transition artifact status on
            workflow failure or to mark processing → completed.
            """
            if request is None:
                request = {}

            new_status = request.get("status", "")
            error_msg = request.get("error")
            valid_statuses = {"pending", "processing", "persisted", "completed", "failed", "needs_review"}
            if new_status not in valid_statuses:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid status '{new_status}'. Valid: {valid_statuses}",
                )

            try:
                if not hasattr(engine, "db_pool") or not engine.db_pool:
                    return {"success": False, "error": "database not available"}

                from nexus_sdk.db.models import CanonicalArtifactRow
                from sqlalchemy import update as sa_update

                update_vals: dict = {"status": new_status}
                if error_msg:
                    update_vals["error"] = error_msg
                if new_status in ("completed", "failed", "needs_review"):
                    update_vals["completed_at"] = datetime.now(timezone.utc)

                async with engine.db_pool() as session:
                    stmt = (
                        sa_update(CanonicalArtifactRow)
                        .where(CanonicalArtifactRow.artifact_id == artifact_id)
                        .values(**update_vals)
                    )
                    await session.execute(stmt)
                    await session.commit()

                    # NOTE: Do NOT invalidate the fingerprint→artifact_id Redis cache here.
                    # Status updates (completed/failed/needs_review) don't change
                    # the artifact identity.  Deleting the cache would break
                    # duplicate-upload detection.

                return {
                    "success": True,
                    "artifact_id": artifact_id,
                    "status": new_status,
                }
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        @app.post("/api/v1/spine/artifacts/{artifact_id}/reuse")
        async def link_reused_artifact(
            artifact_id: str,
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Link a cache-hit artifact to a new session without reprocessing."""
            if request is None:
                request = {}

            tenant_id = request.get("tenant_id") or user.tenant_id
            session_id = request.get("session_id", "")
            if not session_id:
                raise HTTPException(status_code=422, detail="session_id required")

            artifact = await _load_artifact_from_db(engine, artifact_id)
            if not artifact:
                raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
            if artifact.get("tenant_id") != tenant_id:
                raise HTTPException(status_code=403, detail="Artifact belongs to a different tenant")

            session_cache_key = f"canonical:artifact:{tenant_id}:{session_id}"
            await _redis_set(
                engine,
                session_cache_key,
                {
                    "artifact_id": artifact_id,
                    "reused": True,
                    "source_session_id": artifact.get("session_id"),
                },
            )

            return {
                "success": True,
                "artifact_id": artifact_id,
                "session_id": session_id,
                "status": "linked",
            }

        # ── P2: Workflow Write-Through Persistence ─────────────

        @app.post("/api/v1/spine/persist-workflow")
        async def persist_workflow(
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Upsert a workflow instance row in PostgreSQL.

            P2: Called by the orchestrator at every stage transition
            so the read model (platform API) is always consistent
            with the runtime state. Uses ON CONFLICT ... DO UPDATE
            to avoid duplicates.
            """
            if request is None:
                request = {}
            workflow_id = request.get("workflow_id")
            if not workflow_id:
                raise HTTPException(status_code=422, detail="workflow_id required")

            try:
                if not hasattr(engine, "db_pool") or not engine.db_pool:
                    return {"success": False, "error": "database not available"}

                from nexus_sdk.db.models import WorkflowInstanceRow
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                row_data = {
                    "workflow_id": workflow_id,
                    "chain_id": request.get("chain_id", ""),
                    "chain_name": request.get("chain_name", ""),
                    "tenant_id": request.get("tenant_id", ""),
                    "session_id": request.get("session_id", ""),
                    "created_by": request.get("created_by", ""),
                    "status": request.get("status", "created"),
                    "input_data": request.get("input_data", {}),
                    "stages": request.get("stages", {}),
                    "timeline": request.get("timeline", []),
                    "error": request.get("error"),
                }
                # Parse ISO datetime strings to datetime objects for DB
                for dt_field in ("started_at", "completed_at"):
                    val = request.get(dt_field)
                    if val and isinstance(val, str):
                        try:
                            row_data[dt_field] = datetime.fromisoformat(val)
                        except (ValueError, TypeError):
                            pass
                    elif val:
                        row_data[dt_field] = val

                async with engine.db_pool() as session:
                    stmt = pg_insert(WorkflowInstanceRow).values(**row_data)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["workflow_id"],
                        set_={
                            "status": stmt.excluded.status,
                            "stages": stmt.excluded.stages,
                            "timeline": stmt.excluded.timeline,
                            "error": stmt.excluded.error,
                            "started_at": stmt.excluded.started_at,
                            "completed_at": stmt.excluded.completed_at,
                        },
                    )
                    await session.execute(stmt)
                    await session.commit()

                return {"success": True, "workflow_id": workflow_id}
            except Exception as exc:
                import logging
                logging.getLogger("spine").warning(
                    "spine.persist_workflow_failed: %s", exc,
                )
                return {"success": False, "error": str(exc)}

        # ── Retrieve Canonical Artifact ────────────────────────

        @app.get("/api/v1/spine/artifacts/{session_id}")
        async def get_canonical_artifact(
            session_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Retrieve the canonical artifact for a KT session."""
            tenant_id = user.tenant_id

            # Try Redis cache first
            cache_key = f"canonical:artifact:{tenant_id}:{session_id}"
            cached = await _redis_get(engine, cache_key)
            if cached and cached.get("artifact_id"):
                artifact = await _load_artifact_from_db(
                    engine, cached["artifact_id"]
                )
                if artifact:
                    return {"success": True, "artifact": artifact}

            # Fall through to DB scan by session
            artifact = await _find_artifact_by_session(
                engine, tenant_id, session_id
            )
            if artifact:
                return {"success": True, "artifact": artifact}

            raise HTTPException(
                status_code=404,
                detail=f"No canonical artifact found for session {session_id}",
            )

        # ── Persist Test Cases ─────────────────────────────────

        @app.post("/api/v1/spine/persist-test-cases")
        async def persist_test_cases(
            request: dict,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Phase 2.3 — Persist generated test cases to PostgreSQL.

            Receives test_cases[] from Heart engine's generate-tests output
            and stores each as a TestCaseRow with steps and traceability.
            """
            tenant_id = request.get("tenant_id") or user.tenant_id
            session_id = request.get("session_id", "")
            artifact_id = request.get("canonical_artifact_id", "")
            test_cases = request.get("test_cases", [])

            if not test_cases:
                return {"success": True, "stored_count": 0, "test_case_ids": []}

            stored_ids = []
            try:
                if not hasattr(engine, "db_pool") or not engine.db_pool:
                    logger.warning("spine.persist_tests: No DB pool available")
                    return {
                        "success": False,
                        "error": "Database not available",
                        "stored_count": 0,
                        "test_case_ids": [],
                    }

                from nexus_sdk.db.models import TestCaseRow, TestCaseStepRow
                import uuid as _uuid

                async with engine.db_pool() as session:
                    for i, tc in enumerate(test_cases):
                        tc_id = tc.get("test_case_id") or f"TC-{_uuid.uuid4().hex[:8].upper()}"
                        row = TestCaseRow(
                            test_case_id=tc_id,
                            tenant_id=tenant_id,
                            title=tc.get("name", tc.get("title", f"Test Case {i+1}")),
                            description=tc.get("description", ""),
                            test_type=tc.get("type", "e2e"),
                            priority=tc.get("priority", "medium"),
                            status="draft",
                            version=1,
                            validates_rules=tc.get("prerequisite_rules", []),
                            source_session_id=session_id,
                            generated_by="system",
                        )
                        session.add(row)

                        # Persist test steps
                        for step_num, step in enumerate(tc.get("steps", []), 1):
                            step_row = TestCaseStepRow(
                                step_id=f"{tc_id}-S{step_num:03d}",
                                test_case_id=tc_id,
                                step_number=step_num,
                                action=step.get("action", ""),
                                expected_result=step.get("expected_result", ""),
                                target_system=step.get("target_system", ""),
                                target_element=step.get("target_element", ""),
                            )
                            session.add(step_row)

                        stored_ids.append(tc_id)

                    await session.commit()

                logger.info(
                    "spine.persist_tests: stored %d test cases for session %s",
                    len(stored_ids), session_id,
                )
                return {
                    "success": True,
                    "stored_count": len(stored_ids),
                    "test_case_ids": stored_ids,
                }
            except Exception as exc:
                logger.exception("spine.persist_tests failed: %s", exc)
                return {
                    "success": False,
                    "error": str(exc),
                    "stored_count": len(stored_ids),
                    "test_case_ids": stored_ids,
                }

        # ── Persist Contradictions ─────────────────────────────

        @app.post("/api/v1/spine/persist-contradictions")
        async def persist_contradictions(
            request: dict,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Phase 2.5 — Persist detected contradictions to PostgreSQL.

            Receives contradictions[] from Brain engine's detect-contradictions
            output and stores each as a ContradictionRow.
            """
            tenant_id = request.get("tenant_id") or user.tenant_id
            session_id = request.get("session_id", "")
            contradictions = request.get("contradictions", [])

            if not contradictions:
                return {"success": True, "stored_count": 0, "contradiction_ids": []}

            stored_ids = []
            try:
                if not hasattr(engine, "db_pool") or not engine.db_pool:
                    logger.warning("spine.persist_contradictions: No DB pool available")
                    return {
                        "success": False,
                        "error": "Database not available",
                        "stored_count": 0,
                        "contradiction_ids": [],
                    }

                from nexus_sdk.db.models import ContradictionRow
                import uuid as _uuid

                async with engine.db_pool() as session:
                    for c in contradictions:
                        c_id = f"CTR-{_uuid.uuid4().hex[:12]}"
                        row = ContradictionRow(
                            contradiction_id=c_id,
                            tenant_id=tenant_id,
                            rule_a_id=c.get("rule_a_id", ""),
                            rule_b_id=c.get("rule_b_id", ""),
                            description=c.get("description", ""),
                            severity=c.get("severity", "medium"),
                            status="open",
                            metadata_json={
                                "rule_a_text": c.get("rule_a_text", ""),
                                "rule_b_text": c.get("rule_b_text", ""),
                                "rule_a_session": c.get("rule_a_session", ""),
                                "rule_b_session": c.get("rule_b_session", ""),
                                "suggested_resolution": c.get("suggested_resolution", ""),
                                "detected_in_session": session_id,
                                "confidence": c.get("confidence", 0.0),
                            },
                        )
                        session.add(row)
                        stored_ids.append(c_id)

                    await session.commit()

                logger.info(
                    "spine.persist_contradictions: stored %d for session %s",
                    len(stored_ids), session_id,
                )
                return {
                    "success": True,
                    "stored_count": len(stored_ids),
                    "contradiction_ids": stored_ids,
                }
            except Exception as exc:
                logger.exception("spine.persist_contradictions failed: %s", exc)
                return {
                    "success": False,
                    "error": str(exc),
                    "stored_count": len(stored_ids),
                    "contradiction_ids": stored_ids,
                }


# ─── Canonical Artifact Helpers ────────────────────────────────


def _resolve_file_path(
    storage_root: str, tenant_id: str, file_id: str
) -> Optional[str]:
    """Resolve an on-disk path for a stored file."""
    # Files are stored under <storage_root>/<tenant_id>/<file_id>
    candidate = Path(storage_root) / tenant_id / file_id
    if candidate.exists():
        return str(candidate)
    # Try flat layout
    candidate = Path(storage_root) / file_id
    if candidate.exists():
        return str(candidate)
    return None


def _ffprobe_file(file_path: str) -> dict[str, Any]:
    """Run ffprobe on a media file and return structured metadata."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            file_path,
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            return {"error": "ffprobe failed", "duration_seconds": 0.0}

        data = json.loads(proc.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])

        duration = float(fmt.get("duration", 0.0))
        audio_codec = None
        video_codec = None
        width = 0
        height = 0

        for s in streams:
            codec_type = s.get("codec_type", "")
            if codec_type == "audio" and not audio_codec:
                audio_codec = s.get("codec_name")
            elif codec_type == "video" and not video_codec:
                video_codec = s.get("codec_name")
                width = int(s.get("width", 0))
                height = int(s.get("height", 0))

        return {
            "duration_seconds": duration,
            "audio_codec": audio_codec,
            "video_codec": video_codec,
            "width": width,
            "height": height,
            "resolution": f"{width}x{height}" if width and height else None,
            "format_name": fmt.get("format_name"),
            "bit_rate": int(fmt.get("bit_rate", 0)),
            "size_bytes": int(fmt.get("size", 0)),
        }
    except FileNotFoundError:
        return {"error": "ffprobe not installed", "duration_seconds": 0.0}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return {"error": "probe failed", "duration_seconds": 0.0}


async def _redis_get(engine: "SpineEngine", key: str) -> Optional[dict]:
    """Try to get a JSON value from Redis cache."""
    try:
        redis = getattr(engine.job_store, '_redis', None) if hasattr(engine, 'job_store') else None
        if redis:
            raw = await redis.get(key)
            if raw:
                return json.loads(raw)
    except Exception:
        pass
    return None


async def _redis_set(
    engine: "SpineEngine", key: str, value: dict, ttl: int = 86400
) -> None:
    """Set a JSON value in Redis cache with TTL (default 24h)."""
    try:
        redis = getattr(engine.job_store, '_redis', None) if hasattr(engine, 'job_store') else None
        if redis:
            serialized = json.dumps(value)
            await redis.set(key, serialized, ex=ttl)
    except Exception:
        pass


async def _persist_artifact_to_db(
    engine: "SpineEngine", data: dict
) -> bool:
    """Persist a canonical artifact to PostgreSQL."""
    try:
        if not hasattr(engine, "db_pool") or not engine.db_pool:
            return False

        from nexus_sdk.db.models import CanonicalArtifactRow
        from sqlalchemy.ext.asyncio import AsyncSession

        async with engine.db_pool() as session:
            transcript = (
                data.get("safe_transcript_text")
                or data.get("raw_transcript_text")
                or ""
            )
            visual = data.get("visual_summary", "")
            scene_count = data.get("scene_count", 0)

            # Phase 2.3: Derive semantic completeness flags
            has_real_transcript = bool(
                transcript
                and len(transcript.split()) >= 10
                and "[stub" not in transcript.lower()
                and "[placeholder" not in transcript.lower()
            )
            has_visual = bool(
                visual and scene_count > 0 and "[stub" not in visual.lower()
            )
            # Weighted score: transcript 50%, visual 30%, metadata 20%
            t_score = min(len(transcript.split()) / 100, 1.0) if has_real_transcript else 0.0
            v_score = min(scene_count / 5, 1.0) if has_visual else 0.0
            m_score = 1.0 if data.get("duration_seconds", 0) > 0 else 0.0
            semantic_score = round(t_score * 0.5 + v_score * 0.3 + m_score * 0.2, 3)

            row = CanonicalArtifactRow(
                artifact_id=data["artifact_id"],
                tenant_id=data["tenant_id"],
                session_id=data["session_id"],
                media_fingerprint=data.get("media_fingerprint"),
                status=data.get("status", "completed"),
                workflow_id=data.get("workflow_id") or None,
                source_type=data.get("source_type") or None,
                source_filename=data.get("source_filename") or None,
                created_by=data.get("created_by") or None,
                duration_seconds=data.get("duration_seconds", 0.0),
                scene_count=scene_count,
                frame_count=data.get("frame_count", 0),
                safe_transcript_text=transcript,
                visual_summary=visual,
                application_types_seen=data.get("application_types_seen", []),
                brain_quality_score=data.get("brain_quality_score"),
                quality_gate_passed=data.get("quality_gate_passed", False),
                quality_gate_outcome=data.get("quality_gate_outcome"),
                has_real_transcript=has_real_transcript,
                has_visual_semantics=has_visual,
                semantic_completeness_score=semantic_score,
                full_artifact_json=data.get("full_artifact_json"),
                processing_time_seconds=data.get("processing_time_seconds"),
            )
            session.add(row)
            await session.commit()
            return True
    except Exception:
        return False


async def _load_artifact_from_db(
    engine: "SpineEngine", artifact_id: str
) -> Optional[dict]:
    """Load a single artifact by ID from PostgreSQL."""
    try:
        if not hasattr(engine, "db_pool") or not engine.db_pool:
            return None

        from nexus_sdk.db.models import CanonicalArtifactRow
        from sqlalchemy import select

        async with engine.db_pool() as session:
            stmt = select(CanonicalArtifactRow).where(
                CanonicalArtifactRow.artifact_id == artifact_id
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if not row:
                return None
            return _row_to_dict(row)
    except Exception:
        return None


async def _find_artifact_by_session(
    engine: "SpineEngine", tenant_id: str, session_id: str
) -> Optional[dict]:
    """Find the latest canonical artifact for a tenant + session pair."""
    try:
        if not hasattr(engine, "db_pool") or not engine.db_pool:
            return None

        from nexus_sdk.db.models import CanonicalArtifactRow
        from sqlalchemy import select

        async with engine.db_pool() as session:
            stmt = (
                select(CanonicalArtifactRow)
                .where(
                    CanonicalArtifactRow.tenant_id == tenant_id,
                    CanonicalArtifactRow.session_id == session_id,
                )
                .order_by(CanonicalArtifactRow.created_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if not row:
                return None
            return _row_to_dict(row)
    except Exception:
        return None


def _row_to_dict(row) -> dict:
    """Convert a CanonicalArtifactRow to a JSON-serializable dict."""
    return {
        "artifact_id": row.artifact_id,
        "tenant_id": row.tenant_id,
        "session_id": row.session_id,
        "media_fingerprint": row.media_fingerprint,
        "status": row.status,
        "workflow_id": getattr(row, "workflow_id", None),
        "source_type": getattr(row, "source_type", None),
        "source_filename": getattr(row, "source_filename", None),
        "created_by": getattr(row, "created_by", None),
        "duration_seconds": row.duration_seconds,
        "scene_count": row.scene_count,
        "frame_count": row.frame_count,
        "safe_transcript_text": row.safe_transcript_text,
        "visual_summary": row.visual_summary,
        "application_types_seen": row.application_types_seen,
        "brain_quality_score": row.brain_quality_score,
        "quality_gate_passed": row.quality_gate_passed,
        "quality_gate_outcome": getattr(row, "quality_gate_outcome", None),
        "has_real_transcript": getattr(row, "has_real_transcript", False),
        "has_visual_semantics": getattr(row, "has_visual_semantics", False),
        "semantic_completeness_score": getattr(row, "semantic_completeness_score", None),
        "full_artifact_json": row.full_artifact_json,
        "processing_time_seconds": row.processing_time_seconds,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "error": row.error,
    }


# ─── Background Processing ────────────────────────────────────

async def _process_document(
    engine: SpineEngine,
    content: bytes,
    filename: str,
    document_id: str,
    tenant_id: str,
    session_id: Optional[str],
    job_id: str,
):
    """Background task: parse, classify, chunk a document."""
    doc_meta = engine.documents[document_id]
    start = time.monotonic()

    try:
        doc_meta["status"] = ParseStatus.PARSING.value
        await engine.job_store.update_job(job_id, status=JobStatus.PROCESSING.value, progress_percent=10.0)

        result = engine.processor.process(
            content=content,
            filename=filename,
            document_id=document_id,
            tenant_id=tenant_id,
            session_id=session_id,
            classification_keywords=CLASSIFICATION_KEYWORDS,
        )

        await engine.job_store.update_job(job_id, progress_percent=60.0)
        doc_meta["status"] = ParseStatus.CHUNKING.value

        chunks = result.get("chunks", [])
        tables = result.get("tables", [])
        engine.document_chunks[document_id] = chunks
        engine.document_tables[document_id] = tables

        await engine.job_store.update_job(job_id, progress_percent=80.0)
        doc_meta["status"] = ParseStatus.CLASSIFYING.value

        doc_type = result.get("document_type", DocumentType.UNKNOWN)
        doc_meta["detected_type"] = doc_type.value if isinstance(doc_type, DocumentType) else str(doc_type)
        doc_meta["page_count"] = result.get("page_count", 0)
        doc_meta["chunk_count"] = len(chunks)
        doc_meta["table_count"] = len(tables)
        doc_meta["file_hash"] = result.get("file_hash", "")

        elapsed_ms = (time.monotonic() - start) * 1000
        doc_meta["processing_time_ms"] = elapsed_ms
        doc_meta["status"] = ParseStatus.COMPLETED.value

        await engine.job_store.update_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            progress_percent=100.0,
            result={
                "document_id": document_id,
                "document_type": doc_meta["detected_type"],
                "page_count": doc_meta["page_count"],
                "chunk_count": len(chunks),
                "table_count": len(tables),
                "processing_time_ms": elapsed_ms,
            },
        )

        if engine.event_bus:
            try:
                await engine.event_bus.publish(NexusEvent(
                    event_type="spine.document.ingested",
                    source="spine",
                    data={
                        "tenant_id": tenant_id,
                        "document_id": document_id,
                        "filename": filename,
                        "document_type": doc_meta["detected_type"],
                        "chunk_count": len(chunks),
                        "table_count": len(tables),
                        "session_id": session_id,
                    },
                ))
            except Exception:
                pass

    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        doc_meta["status"] = ParseStatus.FAILED.value
        doc_meta["error"] = str(e)
        doc_meta["processing_time_ms"] = elapsed_ms
        await engine.job_store.update_job(
            job_id,
            status=JobStatus.FAILED.value,
            error=str(e),
        )


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    SpineEngine().run()


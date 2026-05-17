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
import re
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
from nexus_sdk.storage import ArtifactStore, StorageConfig, create_storage

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


# ─── PII Redaction for Visual Data (Defense-in-Depth) ──────────

# Critical PII patterns applied to visual text (OCR, descriptions)
# before persisting canonical artifacts. This is a defense-in-depth
# layer — Shield handles full redaction for transcripts, but visual
# data (screenshots, frame OCR) bypasses Shield and could contain
# visible PII like SSNs, phone numbers, or policy numbers.
_VISUAL_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r'\b\d{3}[- ]?\d{2}[- ]?\d{4}\b')),
    ("PHONE", re.compile(r'\b(?:\+?1[- ]?)?\(?\d{3}\)?[- .]?\d{3}[- .]?\d{4}\b')),
    ("EMAIL", re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')),
    ("CREDIT_CARD", re.compile(r'\b(?:\d[ -]*?){13,16}\b')),
    ("DATE_OF_BIRTH", re.compile(
        r'\b(?:DOB|D\.O\.B\.?|Date of Birth|Birth ?Date)[:\s]*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b',
        re.IGNORECASE,
    )),
    ("POLICY_NUMBER", re.compile(
        r'\b(?:Policy|Pol)[#. ]*(?:No|Num|Number)?[#.: ]*[A-Z]{0,3}\d{6,12}\b',
        re.IGNORECASE,
    )),
]


def _redact_pii_in_text(text: str) -> str:
    """Apply critical PII pattern redaction to a single text string."""
    if not text:
        return text
    redacted = text
    for pii_type, pattern in _VISUAL_PII_PATTERNS:
        redacted = pattern.sub(f"[REDACTED_{pii_type}]", redacted)
    return redacted


def _redact_pii_in_visual_data(artifact_data: dict) -> None:
    """Redact PII from visual text fields in a canonical artifact (in-place).

    Applies to:
      - visual_summary (top-level text)
      - Each frame's extracted_text in full_artifact_json.visual_analysis.frames
      - Each frame's description (LLaVA may mention visible PII)
      - Visual graph node ocr_sample fields
    """
    # Redact top-level visual summary
    if artifact_data.get("visual_summary"):
        artifact_data["visual_summary"] = _redact_pii_in_text(
            artifact_data["visual_summary"]
        )

    # Redact within full_artifact_json
    full_json = artifact_data.get("full_artifact_json") or {}

    # Visual analysis frames
    visual_analysis = full_json.get("visual_analysis") or {}
    for frame in visual_analysis.get("frames", []):
        if frame.get("extracted_text"):
            frame["extracted_text"] = _redact_pii_in_text(frame["extracted_text"])
        if frame.get("description"):
            frame["description"] = _redact_pii_in_text(frame["description"])

    # Visual graph node OCR samples
    visual_graph = full_json.get("visual_graph") or {}
    graph = visual_graph.get("graph") or {}
    for node in graph.get("nodes", []):
        if node.get("ocr_sample"):
            node["ocr_sample"] = _redact_pii_in_text(node["ocr_sample"])


# ─── Synthetic evidence_step helpers ──────────────────────────
#
# The triangulator emits steps only when a frame-pair diff fires a visual
# signal (OCR diff, cursor click, LLaVA delta). Two categories of real
# visual evidence escape that net and need their own synthesis pass so
# the bottom panel actually shows what the user did:
#
#   1. Filled controls — the eyes engine extracted a UI control whose
#      value field is populated. The field showing a value IS evidence
#      that the user selected/typed something. Single-frame scenes,
#      where there is no frame pair to diff, otherwise lose this.
#
#   2. Outgoing flow edges — the user transitioning from scene S to
#      scene T captures an action that fired right at the seam between
#      the two scenes (clicking Continue, navigating to the next form
#      step). The triangulator is scene-internal and never sees it.
#
# Both helpers return rows in the same dict shape that
# triangulated_actions_to_step_rows() emits, so the downstream
# bulk-insert path is unchanged.

# Map control element_type → canonical action_kind for synthesised steps.
# Mirrors the SDK control_extractor mapping but kept local so the spine
# does not silently break if the SDK adds a new element_type without
# updating this table.
_ELEMENT_TYPE_TO_ACTION = {
    "button": "click_cta",
    "link": "click_cta",
    "text_field": "enter_text",
    "input": "enter_text",
    "textarea": "enter_text",
    "dropdown": "select_option",
    "select": "select_option",
    "radio": "select_option",
    "checkbox": "toggle",
    "switch": "toggle",
    "tab": "navigate",
    "step_indicator": "navigate",
    "breadcrumb": "navigate",
    "file_input": "upload_file",
}


def _synthesize_steps_from_filled_controls(
    *,
    scenes_rows: list[dict],
    controls_by_scene: dict[str, list[dict]],
    frames_by_id: dict[str, dict],
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    existing_keys: set[tuple[str, str]],
) -> list[dict]:
    """Emit one evidence_step per scene control with a non-empty value.

    The control_id is recorded as ``trigger_control_id`` so the UI can
    link back to the originating ``evidence_controls`` row, and so a
    re-run is idempotent: ``existing_keys`` is the set of
    ``(scene_id, control_id)`` pairs already covered by the triangulator
    and skipped here to avoid duplicates.
    """
    rows: list[dict] = []
    scenes_by_id = {s["scene_id"]: s for s in scenes_rows}
    for scene_id, controls in controls_by_scene.items():
        scene = scenes_by_id.get(scene_id)
        if not scene:
            continue
        # Stable per-scene index counter so step_index makes sense even
        # when only synthetic steps exist on this scene.
        step_index = sum(1 for k in existing_keys if k[0] == scene_id)
        for ctrl in controls:
            value = (ctrl.get("value_text") or "").strip() or (
                ctrl.get("observed_value") or ""
            ).strip()
            if not value:
                continue
            label = (ctrl.get("label_text") or "").strip()
            if not label:
                continue
            control_id = ctrl.get("control_id") or ""
            if (scene_id, control_id) in existing_keys:
                continue
            # Generic quality filter: drop synth steps whose label/value
            # don't look like a real form interaction (OCR row-dumps,
            # body-text fragments, page-tab-title clippings).
            if not _is_meaningful_step(label, value):
                continue
            action_kind = (
                (ctrl.get("action_kind") or "").strip()
                or _ELEMENT_TYPE_TO_ACTION.get(
                    (ctrl.get("element_type") or "").lower(),
                    "ui_interaction",
                )
            )
            # Frame anchors: use the control's frame_id when present;
            # otherwise the scene's first/last frame so before/after
            # thumbnails still render.
            anchor_frame = ctrl.get("frame_id") or ""
            scene_frame_ids = [
                fid for fid, f in frames_by_id.items()
                if f.get("scene_id") == scene_id
            ]
            before_fid = anchor_frame or (scene_frame_ids[0] if scene_frame_ids else None)
            after_fid = anchor_frame or (scene_frame_ids[-1] if scene_frame_ids else None)
            rows.append({
                "step_id": str(uuid.uuid5(
                    uuid.NAMESPACE_OID,
                    f"step:filled-control:{artifact_id}:{scene_id}:{control_id}",
                )),
                "artifact_id": artifact_id,
                "scene_id": scene_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "step_index": step_index,
                "action_kind": action_kind,
                "target_label": label[:500],
                "observed_value": value[:2000],
                "trigger_control_id": control_id or None,
                "before_frame_id": before_fid,
                "after_frame_id": after_fid,
                "start_ms": int(scene.get("start_ms") or 0),
                "end_ms": int(scene.get("end_ms") or scene.get("start_ms") or 0),
                # Filled controls are direct visual facts; we mark them
                # as high-confidence single-source so the UI ribbon is
                # honest about what backed the step.
                "confidence": 0.85,
                "agreement_score": 0.25,
                "evidence_signals": ["filled_control"],
                "cursor_x": None,
                "cursor_y": None,
                "audio_intent_text": "",
                "audio_intent_ts_ms": None,
                "metadata_json": {
                    "source": "filled_control_synthesis",
                    "provenance": {
                        "target_label": {"source": "control_extractor", "confidence": 0.85},
                        "observed_value": {"source": "control_extractor", "confidence": 0.85},
                        "action_kind": {"source": "control_extractor", "confidence": 0.85},
                    },
                },
            })
            existing_keys.add((scene_id, control_id))
            step_index += 1
    return rows


async def _load_visual_flow_edges_for_artifact(
    engine, tenant_id: str, artifact_id: str,
) -> list[dict]:
    """Read visual_flow_edges for an artifact and return as plain dicts."""
    from nexus_sdk.db.models import VisualFlowEdgeRow
    from sqlalchemy import select as sa_select, text as sa_text
    out: list[dict] = []
    async with engine.db_pool() as load_session:
        if tenant_id:
            await load_session.execute(
                sa_text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
                {"tid": tenant_id},
            )
        rows = await load_session.execute(
            sa_select(VisualFlowEdgeRow).where(
                VisualFlowEdgeRow.artifact_id == artifact_id
            )
        )
        for e in rows.scalars().all():
            summary = e.primary_action_summary or {}
            out.append({
                "edge_id": e.edge_id,
                "from_scene_id": e.from_scene_id,
                "to_scene_id": e.to_scene_id,
                "action_type": e.action_type or "",
                "action_value": e.action_value or "",
                "edge_type": e.edge_type or "",
                "transition_ms": int(e.transition_ms or 0),
                "trigger_control_id": e.trigger_control_id,
                "action_quality": e.action_quality or "weak",
                "action_confidence": float(e.action_confidence or 0.0),
                "primary_action_summary": summary if isinstance(summary, dict) else {},
            })
    return out


def _synthesize_steps_from_outgoing_edges(
    *,
    scenes_rows: list[dict],
    edges: list[dict],
    frames_by_id: dict[str, dict],
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    existing_step_rows: list[dict],
) -> list[dict]:
    """Emit one evidence_step per outgoing edge as 'what left this scene'.

    Skipped when:
      * action_quality == 'weak' (no reliable action signal on the edge)
      * the from-scene already has at least one evidence_step that
        covers this transition (matched by trigger_control_id or by
        existing action_kind == this edge's implied kind)
    """
    rows: list[dict] = []
    scenes_by_id = {s["scene_id"]: s for s in scenes_rows}
    steps_by_scene: dict[str, list[dict]] = {}
    for r in existing_step_rows:
        steps_by_scene.setdefault(r["scene_id"], []).append(r)

    for edge in edges:
        if (edge.get("action_quality") or "weak") == "weak":
            continue
        from_id = edge.get("from_scene_id") or ""
        to_id = edge.get("to_scene_id") or ""
        if not from_id or from_id == to_id:
            continue
        from_scene = scenes_by_id.get(from_id)
        to_scene = scenes_by_id.get(to_id)
        if not from_scene or not to_scene:
            continue

        # Skip if the from-scene already has a step linking to this
        # transition's trigger_control_id — the triangulator covered it.
        trig = edge.get("trigger_control_id") or ""
        if trig and any(
            (r.get("trigger_control_id") or "") == trig
            for r in steps_by_scene.get(from_id, [])
        ):
            continue

        summary = edge.get("primary_action_summary") or {}
        action_kind = (
            (summary.get("action_kind") or "").strip()
            or _normalize_edge_action_kind(edge.get("action_type") or "")
        )
        target_label = (
            (summary.get("action_label") or "").strip()
            or (to_scene.get("screen_name") or "")[:120]
        )
        observed_value = (edge.get("action_value") or "")[:2000]
        step_index = len(steps_by_scene.get(from_id, []))
        edge_id = edge.get("edge_id") or ""
        # Frame anchors so the bottom panel can render before/after
        # thumbnails. "before" = the last frame of the from-scene (page
        # the user was leaving); "after" = the first frame of the to-scene
        # (page they landed on). Falls back to None when the scene has no
        # frames assigned, which produces a "no frame" placeholder in the
        # UI rather than a broken image.
        from_frames = sorted(
            [
                (f.get("frame_index", 0), fid)
                for fid, f in frames_by_id.items()
                if f.get("scene_id") == from_id
            ],
            key=lambda t: t[0],
        )
        to_frames = sorted(
            [
                (f.get("frame_index", 0), fid)
                for fid, f in frames_by_id.items()
                if f.get("scene_id") == to_id
            ],
            key=lambda t: t[0],
        )
        edge_before = from_frames[-1][1] if from_frames else None
        edge_after = to_frames[0][1] if to_frames else None
        rows.append({
            "step_id": str(uuid.uuid5(
                uuid.NAMESPACE_OID,
                f"step:outgoing-edge:{artifact_id}:{from_id}:{edge_id}",
            )),
            "artifact_id": artifact_id,
            "scene_id": from_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "step_index": step_index,
            "action_kind": action_kind or "ui_interaction",
            "target_label": target_label[:500],
            "observed_value": observed_value[:2000],
            "trigger_control_id": trig or None,
            "before_frame_id": edge_before,
            "after_frame_id": edge_after,
            "start_ms": int(from_scene.get("end_ms") or 0),
            "end_ms": int(to_scene.get("start_ms") or 0),
            "confidence": float(edge.get("action_confidence") or 0.7),
            "agreement_score": 0.25,
            "evidence_signals": ["flow_edge"],
            "cursor_x": None,
            "cursor_y": None,
            "audio_intent_text": "",
            "audio_intent_ts_ms": None,
            "metadata_json": {
                "source": "outgoing_edge_synthesis",
                "edge_id": edge_id,
                "edge_action_type": edge.get("action_type") or "",
                "to_scene_id": to_id,
            },
        })
        steps_by_scene.setdefault(from_id, []).append(rows[-1])
    return rows


def _is_meaningful_step(target_label: str, observed_value: str) -> bool:
    """Generic quality filter for evidence_step target/value pairs.

    Rejects steps whose fields are obviously OCR row-dumps, page-body
    fragments, or self-equal noise — independent of any specific domain.
    Returns True when the pair is *plausibly* a real user action; False
    when it should be silently dropped before persistence.

    The rules are structural, not lexical:
      * ``target == value`` (after strip) — always OCR fingerprint on
        both sides ("senter youf" / "senter youf"), never a real action.
      * Either field has 4+ tokens with majority lowercase — body-text
        prose ("increase continue usually factor older rates"), not a
        form field label/value.
      * Either field is multi-token and *all* tokens start lowercase —
        catches user-name patterns ("ramanjaneya venkata") and OCR noise
        strings; real form values are either single-token, Title-case,
        or numeric.
      * Field is just punctuation + digits with no useful content
        (". 5", "/ 12") — chrome fragment from a clock/time widget.
    """
    tl = (target_label or "").strip()
    ov = (observed_value or "").strip()
    if not tl and not ov:
        return False
    if tl and ov and tl == ov:
        return False
    for text in (tl, ov):
        if not text:
            continue
        tokens = text.split()
        if not tokens:
            continue
        # Strip wrapping punctuation per token for the structural checks.
        cleaned = [t.strip(",.;:!?\"'()[]{}-/$%") for t in tokens]
        cleaned = [t for t in cleaned if t]
        if not cleaned:
            return False
        # Long body-text prose: 4+ tokens with any meaningful lowercase
        # presence. Real form labels are short and Title-case throughout;
        # 30% lowercase across 4+ tokens almost always indicates either
        # body content ("Ask Gemini guardianlife com") or truncated
        # multi-line OCR being concatenated into one phrase.
        if len(cleaned) >= 4:
            n_lower = sum(1 for t in cleaned if t[:1].islower())
            if n_lower * 10 >= len(cleaned) * 3:
                return False
            # Trailing short-and-lowercase token is an OCR truncation
            # tail ("PM Border Patrol chi" — "chi" is junk).
            tail = cleaned[-1]
            if tail and len(tail) < 4 and tail[:1].islower():
                return False
            # 4+ all-Title-case tokens reads as a page heading / panel
            # title ("Term Life Insurance Calculator"), not a form field
            # label or value. Real labels and values are at most a few
            # words; phrases this long are page-level captions.
            if all(t[:1].isupper() for t in cleaned):
                return False
        # Multi-token all-lowercase — real form values aren't shaped this
        # way (single-word values, Title-case phrases, numerics dominate).
        if len(cleaned) >= 2 and all(t[:1].islower() for t in cleaned):
            return False
        # Field is just digits/short punct: chrome artefact from clock/
        # weather widget OCR ("8 25 PM", ". 5").
        all_digit_or_short = all(
            t.isdigit() or (len(t) == 1 and not t.isalnum())
            for t in cleaned
        )
        if all_digit_or_short and not any(t.isdigit() and len(t) >= 2 for t in cleaned):
            return False
    return True


def _normalize_edge_action_kind(action_type: str) -> str:
    """Map visual_flow_edges.action_type to canonical action_kind."""
    t = (action_type or "").strip().lower()
    if t in ("navigate", "click", "click_cta", "select_option", "enter_text",
            "submit_form", "open_overlay", "close_overlay", "review", "scroll",
            "toggle"):
        if t == "click":
            return "click_cta"
        return t
    if t == "transition":
        return "navigate"
    return "ui_interaction"


# Words that look like form-field values but aren't — placeholder text,
# instructions, generic UI chrome. If a candidate value matches one of
# these we keep walking past it. Single-word entries only — multi-word
# placeholders get caught by the "looks like a label or instruction"
# heuristic in _looks_like_value below.
_VALUE_BLOCKLIST = frozenset({
    "select", "selected", "please", "choose", "pick", "your", "the", "a", "an",
    "required", "optional", "enter", "type", "fill", "input",
    "yes", "no",  # ambiguous — too generic to be a confident pick
    "submit", "cancel", "back", "next", "continue", "save", "close",
    "and", "or", "of", "for", "to", "from", "with", "by", "on", "in", "at",
    "this", "that", "these", "those", "is", "are", "was", "were", "be",
    "ask", "gemini", "search", "find", "more", "help", "show", "hide",
})


# Known value sets — when the candidate value matches one of these, score
# the match as high-confidence regardless of capitalisation or position.
_KNOWN_VALUES_BY_LABEL_KEYWORD: dict[str, frozenset[str]] = {
    "gender": frozenset({"male", "female", "other", "non-binary", "nonbinary",
                         "prefer not to say"}),
    "state": frozenset({
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada", "ohio",
        "oklahoma", "oregon", "pennsylvania", "rhode island",
        "south carolina", "south dakota", "tennessee", "texas", "utah",
        "vermont", "virginia", "washington", "west virginia", "wisconsin",
        "wyoming", "district of columbia",
    }),
}


def _label_value_set(label: str) -> Optional[frozenset[str]]:
    """Return the known-value set for a label, if its lowercase form contains
    a keyword matching one of the entries in ``_KNOWN_VALUES_BY_LABEL_KEYWORD``.
    """
    if not label:
        return None
    lower = label.lower()
    for keyword, values in _KNOWN_VALUES_BY_LABEL_KEYWORD.items():
        if keyword in lower:
            return values
    return None


def _looks_like_value(token: str) -> bool:
    """Heuristic: does ``token`` look like a form-field value?

    Acceptable shapes:
      * Capitalised word, length 2..40, alphanumeric.
      * Numeric ($ amounts, ages, etc.) — at least one digit.
      * Multi-token phrases handled by the caller (joining adjacent
        capitalised tokens until a stop word is reached).

    Reject obviously bad shapes:
      * Block-listed generic words (case-insensitive).
      * Single character or all-punctuation.
      * Tokens that are dominantly non-alphanumeric (OCR garbage).
    """
    if not token:
        return False
    stripped = token.strip(",.;:!?\"'()[]{}-/")
    if not stripped:
        return False
    lower = stripped.lower()
    if lower in _VALUE_BLOCKLIST:
        return False
    # Numeric value (income, age, etc.)
    if any(ch.isdigit() for ch in stripped):
        # require at least one digit AND mostly digit/punctuation/$ chars
        n_alpha = sum(1 for c in stripped if c.isalpha())
        n_digit = sum(1 for c in stripped if c.isdigit())
        if n_digit >= 1 and n_digit >= n_alpha:
            return True
        return False
    # Alpha word — require Title-case or full ALL-CAPS, length 2..40.
    if not (2 <= len(stripped) <= 40):
        return False
    if not stripped[:1].isupper():
        return False
    # Reject words that are mostly non-letters (OCR noise).
    if sum(1 for c in stripped if c.isalpha()) < len(stripped) * 0.7:
        return False
    return True


def _extract_value_after_label(
    ocr_text: str,
    label: str,
    other_labels_lower: set[str],
    *,
    window: int = 8,
) -> str:
    """Find ``label`` in ``ocr_text`` and return the most plausible value
    that follows it within the next ``window`` tokens.

    Match is **word-boundary-anchored** so the label "Age" doesn't
    accidentally match the "age" inside a garbled OCR run like
    "Ihjemale". Then tokens after the label are walked while:

      * Skipping placeholder/instruction words (``_VALUE_BLOCKLIST``).
      * Skipping tokens that appear as a word in ANY other control's
        label (catches the case where "Get" gets picked as a value
        even though "Get my quote" is a button several tokens later).
      * Stopping at the start of another known label.

    Layer A (known value sets like Male/Female/state names) wins over
    Layer B (generic plausible-value) so a clear gender/state pick
    survives even when the surrounding text is noisy.

    Returns the extracted value (1–2 tokens) or "" when nothing
    plausible is found.
    """
    if not ocr_text or not label:
        return ""

    text_lower = ocr_text.lower()
    label_lower = label.lower().strip()
    if not label_lower:
        return ""

    # Word-boundary search so the label "Age" doesn't match inside
    # garbled OCR like "Ihjemale". For multi-word labels the boundary
    # is anchored to the whole phrase.
    label_pattern = r"\b" + re.escape(label_lower) + r"\b"
    m = re.search(label_pattern, text_lower)
    if m is None:
        # Fall back to the label's first word if it's distinctive (>=5
        # chars) and not a common stop word.
        words = label_lower.split()
        first_word = words[0] if words else ""
        if len(first_word) >= 5 and first_word not in _VALUE_BLOCKLIST:
            m = re.search(r"\b" + re.escape(first_word) + r"\b", text_lower)
        if m is None:
            return ""

    label_end = m.end()
    after = ocr_text[label_end:label_end + 400]
    tokens = re.findall(r"\S+", after)

    # Build a set of every word that appears in any *other* control
    # label, so a candidate that's actually the first/inner word of
    # another field's label gets rejected.
    other_label_words: set[str] = set()
    for ol in other_labels_lower:
        for w in re.findall(r"[a-z0-9]+", ol):
            if len(w) >= 3 and w not in _VALUE_BLOCKLIST:
                other_label_words.add(w)

    # Layer A — known value set (highest reliability). Scan a wider
    # window because placeholder text often sits between the label and
    # the actual selected value ("Gender Select your gender Male").
    known = _label_value_set(label)
    if known:
        for tok in tokens[:window * 2]:
            cleaned = tok.strip(",.;:!?\"'()[]{}-/").lower()
            if cleaned in known:
                return cleaned.title()
        # Two-token phrases for multi-word values (state names like
        # "New York", "Rhode Island").
        for i in range(min(len(tokens), window * 2) - 1):
            two = (
                tokens[i].strip(",.;:!?\"'()[]{}-/").lower()
                + " "
                + tokens[i + 1].strip(",.;:!?\"'()[]{}-/").lower()
            )
            if two in known:
                return two.title()
        # Known set defined but no match — field is empty, return "".
        return ""

    # Layer B — numeric value for labels that strongly imply a number
    # (Age, Income, Year, Count, Quantity, Amount, Salary, …). Without
    # this guard the extractor over-emits, picking the next capitalised
    # noun on the page (page chrome like "Thunderstorm", section
    # headers, footer links) as a "value".
    if _label_implies_numeric(label):
        for tok in tokens[:window]:
            cleaned = tok.strip(",.;:!?\"'()[]{}-/")
            if not cleaned:
                continue
            if cleaned.lower() in other_label_words:
                continue
            # Require at least one digit; allow $ and commas.
            digits = sum(1 for c in cleaned if c.isdigit())
            non_digit_non_punct = sum(
                1 for c in cleaned if c.isalpha()
            )
            if digits >= 1 and non_digit_non_punct == 0:
                return cleaned
        return ""

    # No known value set, label doesn't imply numeric → too ambiguous
    # to pattern-match safely. Returning "" here is the right call —
    # the bottom panel will simply not show a synthetic step for this
    # control, which is far better than emitting "Thunderstorm" for
    # "Annual income".
    _unused = other_label_words  # quieten linter when both layers miss
    return ""


# Label keyword hints that the field expects a numeric value. Used to
# gate the numeric-pattern layer in _extract_value_after_label.
_NUMERIC_LABEL_KEYWORDS = frozenset({
    "age", "income", "salary", "amount", "quantity", "count", "year",
    "month", "day", "date", "phone", "zip", "postal", "ssn", "tax",
    "price", "cost", "balance", "total", "subtotal", "discount", "fee",
    "rate", "percentage", "percent", "annual", "monthly", "weekly",
    "daily", "hourly", "premium", "deductible", "coverage",
})


def _label_implies_numeric(label: str) -> bool:
    """True when the label's wording strongly suggests a numeric value."""
    if not label:
        return False
    words = re.findall(r"[a-z]+", label.lower())
    return any(w in _NUMERIC_LABEL_KEYWORDS for w in words)


def _synthesize_steps_from_ocr_form_match(
    *,
    scenes_rows: list[dict],
    controls_by_scene: dict[str, list[dict]],
    frames_by_id: dict[str, dict],
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    existing_keys: set[tuple[str, str]],
) -> list[dict]:
    """Emit evidence_steps by pattern-matching form values in scene OCR.

    Bridges the OCR-quality gap on Zoom-compressed videos: even when the
    eyes engine cannot reliably extract a ``value_text`` for a control,
    the rendered selected value usually IS somewhere in the scene's
    ocr_text dump because OCR scans the whole frame. We search for the
    control's label and grab the most plausible value-shaped token after
    it. See ``_extract_value_after_label`` for the heuristic.

    Skipped when:
      * the scene already has a step for this control (existing_keys).
      * the control has no usable label_text.
      * no value-shaped token is found within the search window.
    """
    rows: list[dict] = []
    scenes_by_id = {s["scene_id"]: s for s in scenes_rows}
    for scene_id, controls in controls_by_scene.items():
        scene = scenes_by_id.get(scene_id)
        if not scene:
            continue
        ocr_text = (scene.get("ocr_text") or "")
        if not ocr_text or not controls:
            continue
        # Build a set of OTHER control labels for the "next-field" guard.
        other_labels = {
            (c.get("label_text") or "").strip().lower()
            for c in controls
            if c.get("label_text")
        }
        step_index = sum(1 for k in existing_keys if k[0] == scene_id)
        for ctrl in controls:
            label = (ctrl.get("label_text") or "").strip()
            element_type = (ctrl.get("element_type") or "").lower()
            # Only run on interactive form fields where a value is meaningful.
            if element_type not in ("dropdown", "select", "radio", "text_field",
                                    "input", "textarea", "checkbox"):
                continue
            if not label:
                continue
            control_id = ctrl.get("control_id") or ""
            if (scene_id, control_id) in existing_keys:
                continue
            # Exclude this label from "other labels" set so we don't reject
            # the label itself when matching its own value.
            other_minus_self = other_labels - {label.lower()}
            value = _extract_value_after_label(
                ocr_text, label, other_minus_self,
            )
            if not value:
                continue
            # Generic quality filter (same rule across all step
            # synthesisers): drop if the label+value pair is structurally
            # an OCR fragment or body-text rather than a form action.
            if not _is_meaningful_step(label, value):
                continue
            action_kind = _ELEMENT_TYPE_TO_ACTION.get(
                element_type, "ui_interaction",
            )
            # Anchor the step to the scene's first/last frame so the bottom
            # panel renders before/after thumbnails. Without these the
            # browser falls through to the "no frame" placeholder (or
            # worse, an IMG with broken src showing alt text like "Frame
            # at 16800 ms").
            scene_frame_ids_sorted = sorted(
                [
                    (f.get("frame_index", 0), fid)
                    for fid, f in frames_by_id.items()
                    if f.get("scene_id") == scene_id
                ],
                key=lambda t: t[0],
            )
            ocr_before = (
                scene_frame_ids_sorted[0][1]
                if scene_frame_ids_sorted else None
            )
            ocr_after = (
                scene_frame_ids_sorted[-1][1]
                if scene_frame_ids_sorted else None
            )
            rows.append({
                "step_id": str(uuid.uuid5(
                    uuid.NAMESPACE_OID,
                    f"step:ocr-form-match:{artifact_id}:{scene_id}:{control_id}",
                )),
                "artifact_id": artifact_id,
                "scene_id": scene_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "step_index": step_index,
                "action_kind": action_kind,
                "target_label": label[:500],
                "observed_value": value[:2000],
                "trigger_control_id": control_id or None,
                "before_frame_id": ocr_before,
                "after_frame_id": ocr_after,
                "start_ms": int(scene.get("start_ms") or 0),
                "end_ms": int(scene.get("end_ms") or scene.get("start_ms") or 0),
                # Pattern-based — less certain than a directly-captured
                # value_text. 0.70 communicates "good but not 0.85 like
                # eyes-captured filled_control".
                "confidence": 0.70,
                "agreement_score": 0.25,
                "evidence_signals": ["ocr_form_match"],
                "cursor_x": None,
                "cursor_y": None,
                "audio_intent_text": "",
                "audio_intent_ts_ms": None,
                "metadata_json": {
                    "source": "ocr_form_match_synthesis",
                    "provenance": {
                        "target_label": {"source": "control_extractor", "confidence": 0.85},
                        "observed_value": {"source": "ocr_pattern_match", "confidence": 0.70},
                        "action_kind": {"source": "element_type", "confidence": 0.80},
                    },
                },
            })
            existing_keys.add((scene_id, control_id))
            step_index += 1
    return rows


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

        self._storage_config = StorageConfig()
        self._artifacts = ArtifactStore(
            create_storage(self._storage_config), self._storage_config
        )

    async def on_startup(self):
        """Initialize document processor, database pool, and inject event bus."""
        # Ensure the monorepo root is on sys.path so Spine can import the
        # orchestrator's pure-Python evidence service modules (build_scenes,
        # app_segmenter, control_extractor, flow_builder).
        import sys as _sys
        import os as _os
        _repo_root = _os.path.abspath(
            _os.path.join(_os.path.dirname(__file__), "..", "..")
        )
        if _repo_root not in _sys.path:
            _sys.path.insert(0, _repo_root)

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

        # Storage backend (object storage) — used for durable original
        # document copies. The local document_storage_path is only used in
        # local-dev mode; in production it is unmounted.
        if self._artifacts.is_local:
            os.makedirs(self.config.document_storage_path, exist_ok=True)
        self.health.set_mode("document_processor", "local")
        self.health.set_mode(
            "document_storage", self._artifacts.backend_name,
        )

        # ── Canonical workflow workers (Phase 1) ───────────────
        # One long-running loop per resource lane. spine.parse is CPU;
        # spine.chunk_and_embed will become GPU in Phase 1.5 when a
        # dedicated embedding model is wired. We start both lanes today
        # so adding the embedding step later is a no-op at startup.
        # The orchestrator URL gates whether these start — when unset
        # (dev without an orchestrator) the legacy /api/v1/spine path
        # remains the only ingress.
        self._workflow_workers: list = []
        orchestrator_url = os.environ.get("NEXUS_ORCHESTRATOR_URL", "")
        if orchestrator_url:
            await self._start_canonical_workflow_workers(orchestrator_url)
        else:
            import logging as _logging
            _logging.getLogger("spine").info(
                "spine.workflow_workers_disabled "
                "reason=NEXUS_ORCHESTRATOR_URL_unset",
            )

    async def _start_canonical_workflow_workers(self, orchestrator_url: str) -> None:
        import asyncio
        import logging as _logging
        _log = _logging.getLogger("spine")
        from nexus_sdk.workflows import (
            StepKind, WorkerConfig, WorkflowWorker, queue_name,
        )
        from app.workflow_handlers import SpineWorkflowHandlers

        token = os.environ.get("NEXUS_WORKER_TOKEN", "")
        handlers = SpineWorkflowHandlers(self)

        cpu_conc = int(os.environ.get("SPINE_WORKER_CONCURRENCY_CPU", "4"))
        gpu_conc = int(os.environ.get("SPINE_WORKER_CONCURRENCY_GPU", "1"))
        for kind in (StepKind.CPU, StepKind.GPU):
            lane = queue_name("spine", kind)
            q = self._build_workflow_lane_queue(lane)
            ok = await q.connect()
            if not ok:
                _log.error("spine.workflow_worker_redis_unreachable lane=%s", lane)
                continue
            worker = WorkflowWorker(
                config=WorkerConfig(
                    engine_name="spine",
                    kind=kind,
                    orchestrator_url=orchestrator_url,
                    auth_token=token,
                    concurrency=cpu_conc if kind == StepKind.CPU else gpu_conc,
                ),
                queue=q,
            )
            handlers.register(worker)
            self._workflow_workers.append(worker)
            asyncio.create_task(
                worker.run(), name=f"workflow_worker.{lane}",
            )
            _log.info(
                "spine.workflow_worker_started lane=%s orchestrator=%s",
                lane, orchestrator_url,
            )

    def _build_workflow_lane_queue(self, lane: str):
        from nexus_sdk.queue import JobQueue

        return JobQueue(
            engine_name=lane,
            redis_host=os.environ.get("REDIS_HOST", "redis"),
            redis_port=int(os.environ.get("REDIS_PORT", "6379")),
            redis_password=os.environ.get("REDIS_PASSWORD", ""),
            redis_db=int(os.environ.get("REDIS_DB", "3")),
        )

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

            # Durable copy of the original document in object storage.
            # Required for compliance (immutable source-of-truth) and for
            # re-ingestion if extraction logic changes downstream.
            artifact_key = ""
            try:
                artifact_key = engine._artifacts.build_key(
                    tenant_id, "spine", document_id, filename,
                )
                await engine._artifacts.upload_bytes(
                    artifact_key, content,
                    metadata={
                        "document_id": document_id,
                        "session_id": session_id or "",
                        "uploaded_by": user.user_id,
                    },
                )
            except Exception as e:
                import logging
                logging.getLogger("spine").warning(
                    "spine.document_upload_failed: %s", e,
                )

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
                "artifact_key": artifact_key,
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

                # P2 Fix: Generate a unique fallback title when page_title
                # and description are both empty.  Without this, all frames
                # with empty titles merge into a single graph node.
                if not title.strip():
                    title = f"{app_type}@{timestamp:.1f}s"

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

            # Phase 3: the caller (spine.persist_minimal_artifact) can
            # request `artifact_status="minimal"` to write a partial
            # row that the UI surfaces with "enriching" badge. The
            # default remains "persisted" for backward compat with
            # the legacy spine.persist_artifact handler.
            # Architect P3: `completed_degraded` joins the recognized set
            # so workflows that finish with degraded_stages get a status
            # the UI can distinguish from a clean `persisted` run.
            artifact_status = str(request.get("artifact_status") or "persisted").lower()
            if artifact_status not in {
                "minimal", "persisted", "completed", "completed_degraded",
            }:
                artifact_status = "persisted"
            artifact_data = {
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "media_fingerprint": fingerprint,
                "status": artifact_status,
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
                # quality_gate fields intentionally omitted here so
                # `_persist_artifact_to_db` runs `_compute_quality_gate`
                # against the derived has_real_transcript/has_visual/
                # semantic_score values. The previous hardcoded
                # (False, None) bypassed compute and caused every new
                # minimal artifact to render as red "Quality gate
                # failed" in the UI, even when the data showed an
                # otherwise clean run.
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
                    # Phase F.6 — full reproducibility manifest.  Captures
                    # the exact toolchain (model versions, SDK versions,
                    # feature flags, env fingerprint, runtime, git commit)
                    # so this artifact can be replayed against the same
                    # stack later or diffed against a future run to
                    # surface what changed.
                    "run_provenance": _capture_run_provenance_safe(
                        artifact_id=artifact_id,
                        chain_id="nexus.canonical-processing",
                        processing_profile=request.get("processing_profile", ""),
                        engine_versions={
                            "ears": raw_transcript.get("model_version", ""),
                            "eyes": visual_analysis.get("model_version", ""),
                        },
                        model_resolved={
                            "ears": raw_transcript.get("model_version", ""),
                            "eyes": visual_analysis.get("model_version", ""),
                        },
                    ),
                },
                "processing_time_seconds": visual_analysis.get(
                    "processing_time_seconds", 0.0
                ),
                "created_at": now.isoformat(),
                "completed_at": None,  # Set by quality gate or status endpoint — not at persist time
            }

            # P0 Fix: Redact PII from visual data before persistence.
            # Shield only processes transcript text — visual OCR text and
            # LLaVA descriptions could contain visible PII from screenshots.
            _redact_pii_in_visual_data(artifact_data)

            # Persist to PostgreSQL — raises on failure so the orchestrator
            # marks artifact_persistence as failed (not silently cached_only).
            await _persist_artifact_to_db(engine, artifact_data)

            # Cache in Redis for fast lookup — but ONLY for fully
            # enriched artifacts. A `minimal` row isn't a final
            # result; cacheing it would cause re-uploads of the
            # same fingerprint to short-circuit to a partial
            # artifact before enrichment finished.
            if fingerprint and artifact_status != "minimal":
                cache_key = f"canonical:artifact:{tenant_id}:{fingerprint}"
                await _redis_set(
                    engine, cache_key, {"artifact_id": artifact_id}
                )

            # Also cache by session (same skip rule).
            if artifact_status != "minimal":
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
                "status": artifact_status,  # echo back so caller knows it's "minimal" vs "persisted"
                "session_id": session_id,
                "workflow_id": workflow_id,
            }

        # ── Phase 3: enrichment update for persist-first artifacts ────
        #
        # Called by `spine.update_artifact_enriched` after the eyes
        # branch (OCR + LLaVA + transitions + build_evidence + visual
        # graph) completes. Updates the already-persisted minimal row
        # with the full visual_analysis + visual_graph payloads and
        # flips status from `minimal` → `persisted`. Also populates
        # the fingerprint dedup cache that persist-minimal skipped.
        @app.post("/api/v1/spine/update-canonical-artifact-enrichment")
        async def update_canonical_artifact_enrichment(
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Apply heavy-enrichment updates to an existing artifact.

            Payload:
              tenant_id, artifact_id (required)
              visual_analysis (dict), visual_graph (dict)
              degraded_stages (list), degraded_reasons (dict)
            """
            if request is None:
                request = {}
            tenant_id = request.get("tenant_id", "")
            artifact_id = request.get("artifact_id", "")
            if not artifact_id:
                raise HTTPException(
                    status_code=400, detail="artifact_id required",
                )
            visual_analysis = request.get("visual_analysis") or {}
            visual_graph = request.get("visual_graph") or {}
            degraded_stages = list(request.get("degraded_stages") or [])
            degraded_reasons = dict(request.get("degraded_reasons") or {})
            # Architect P3: callers can request a specific terminal
            # artifact status. When the workflow ran with degraded stages,
            # the canonical handler passes `completed_degraded` so the UI
            # can distinguish "fully enriched" from "best-effort fallback"
            # artifacts. Defaults to `persisted` for clean runs.
            requested_status = request.get("artifact_status") or (
                "completed_degraded" if degraded_stages else "persisted"
            )

            # Build a partial update for the canonical_artifacts row.
            visual_summary = ""
            frames = visual_analysis.get("frames", [])
            if frames:
                descriptions = [
                    f.get("description", "") for f in frames if f.get("is_keyframe")
                ]
                visual_summary = " → ".join(d for d in descriptions if d)[:2000]

            # The DB-write function `_update_artifact_enrichment_in_db`
            # recomputes has_real_transcript / has_visual_semantics /
            # semantic_completeness_score / quality_gate from the
            # existing row + the patch we send below. Keep this endpoint
            # body thin so quality-gate computation lives in one place.
            update_fields = {
                "status": requested_status,
                "scene_count": visual_analysis.get("total_frames_analyzed", 0),
                "frame_count": visual_analysis.get("total_frames_extracted", 0),
                "visual_summary": visual_summary,
                "application_types_seen": visual_analysis.get(
                    "application_types_seen", []
                ),
            }
            # Merge visual data into full_artifact_json (JSONB merge).
            full_json_patch = {
                "visual_analysis": visual_analysis,
                "visual_graph": visual_graph,
                "degraded_stages": degraded_stages,
                "degraded_reasons": degraded_reasons,
            }

            await _update_artifact_enrichment_in_db(
                engine, tenant_id, artifact_id, update_fields, full_json_patch,
            )

            # Now that the artifact is fully enriched, populate the
            # fingerprint dedup cache so re-uploads short-circuit
            # correctly. Need to re-read the row to get the fingerprint.
            try:
                row = await _read_canonical_artifact(engine, tenant_id, artifact_id)
                if row and row.get("media_fingerprint"):
                    cache_key = f"canonical:artifact:{tenant_id}:{row['media_fingerprint']}"
                    await _redis_set(engine, cache_key, {"artifact_id": artifact_id})
                if row and row.get("session_id"):
                    sess_key = f"canonical:artifact:{tenant_id}:{row['session_id']}"
                    await _redis_set(engine, sess_key, {"artifact_id": artifact_id})
            except Exception as e:
                logger.warning(
                    "update_canonical_artifact_enrichment: cache_populate_failed err=%s",
                    e,
                )

            # Emit event so consumers know the artifact reached its
            # full state (they can re-render, refresh search indexes,
            # etc.).
            if engine.event_bus:
                try:
                    await engine.event_bus.publish(NexusEvent(
                        event_type="spine.canonical_artifact.enriched",
                        source="spine",
                        data={
                            "tenant_id": tenant_id,
                            "artifact_id": artifact_id,
                            "degraded_stages": degraded_stages,
                        },
                    ))
                except Exception:
                    pass

            return {
                "success": True,
                "artifact_id": artifact_id,
                "status": "persisted",
                "degraded_stages": degraded_stages,
            }

        # ── Persist Visual Evidence Frames ────────────────────

        @app.post("/api/v1/spine/persist-visual-frames")
        async def persist_visual_frames(
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Persist all visual evidence for a canonical artifact (Phases 2–7).

            Called by the canonical chain's persist_visual_evidence stage.
            Executes the full evidence pipeline in one DB transaction:

            1. Persist raw frame rows (Phase 2) — idempotent via ON CONFLICT.
            2. Build scenes from frames using dHash Hamming grouping (Phase 3).
            3. Persist visual_scenes + back-fill visual_frames.scene_id (Phase 3).
            4. Segment scenes into app instances via heuristics (Phase 5).
            5. Persist app_instances + back-fill visual_scenes.app_instance_id (Phase 5).
            6. Extract automation-ready UI controls per scene (Phase 6).
            7. Persist evidence_controls (Phase 6).
            5b. Segment scenes into business-process flows (Phase 5b).
            5b-persist. Persist visual_flows + back-fill visual_scenes.flow_id.
            8. Build Layer-1 flow graph (observed transitions) (Phase 7).
            9. Enrich with Layer-2 action evidence (Phase 7).
            10. Persist visual_flow_edges with flow_id (Phase 7).

            Failure on any evidence phase is non-fatal: the endpoint logs
            the error and returns partial results so the canonical pipeline
            always completes and produces an artifact.

            Request schema:
                tenant_id:   str        — tenant scope
                session_id:  str        — recording session
                artifact_id: str        — pre-allocated canonical artifact id
                frames:      list[dict] — FrameAnalysis objects from Eyes

            Returns:
                {
                    "success": bool,
                    "frames_persisted": int,
                    "scenes_persisted": int,
                    "app_instances_persisted": int,
                    "controls_persisted": int,
                    "flow_edges_persisted": int,
                    "flows_persisted": int,
                    "artifact_id": str,
                    "errors": list[str],   # non-fatal per-phase error strings
                }
            """
            if request is None:
                request = {}

            tenant_id = request.get("tenant_id", "")
            session_id = request.get("session_id", "")
            artifact_id = request.get("artifact_id", "")
            frames: list = request.get("frames") or []
            scene_transitions_req = request.get("scene_transitions") or []
            scene_transitions: list[dict] = []
            for item in scene_transitions_req:
                if isinstance(item, dict):
                    scene_transitions.append(item)
                elif item is not None and hasattr(item, "model_dump"):
                    scene_transitions.append(item.model_dump())

            if not frames:
                return {
                    "success": True,
                    "frames_persisted": 0,
                    "scenes_persisted": 0,
                    "app_instances_persisted": 0,
                    "controls_persisted": 0,
                    "flow_edges_persisted": 0,
                    "flows_persisted": 0,
                    "artifact_id": artifact_id,
                    "message": "No frames supplied — nothing persisted",
                    "errors": [],
                }

            if not hasattr(engine, "db_pool") or not engine.db_pool:
                return {
                    "success": False,
                    "frames_persisted": 0,
                    "scenes_persisted": 0,
                    "app_instances_persisted": 0,
                    "controls_persisted": 0,
                    "flow_edges_persisted": 0,
                    "flows_persisted": 0,
                    "artifact_id": artifact_id,
                    "error": "database not available",
                    "errors": ["database not available"],
                }

            from nexus_sdk.db.models import (
                VisualFrameRow, VisualSceneRow, AppInstanceRow,
                EvidenceControlRow, VisualFlowEdgeRow, VisualFlowRow,
                CanonicalArtifactRow,
            )
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy import update as sa_update, delete as sa_delete, select as sa_select

            # ── Preflight: verify canonical_artifacts parent row exists ──
            # canonical_artifacts has Row-Level Security enabled.
            # The preflight SELECT must set nexus.current_tenant_id so
            # the RLS policy allows the row to be visible.  Without it
            # the query always returns None even though the row exists,
            # causing a false-positive 409 and the entire persist stage
            # to be silently skipped (on_failure="skip" in the chain).
            if artifact_id:
                try:
                    from sqlalchemy import text as sa_text
                    async with engine.db_pool() as _chk_session:
                        # Set RLS tenant scope for this transaction.
                        # NOTE: asyncpg raises ProgrammingError for
                        # "SET LOCAL var = $1" — use set_config() which
                        # accepts bound parameters and is equivalent to
                        # SET LOCAL when is_local=true.
                        if tenant_id:
                            await _chk_session.execute(
                                sa_text(
                                    "SELECT set_config('nexus.current_tenant_id', :tid, true)"
                                ),
                                {"tid": tenant_id},
                            )
                        _exists = await _chk_session.scalar(
                            sa_select(CanonicalArtifactRow.artifact_id).where(
                                CanonicalArtifactRow.artifact_id == artifact_id
                            ).limit(1)
                        )
                    if not _exists:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"canonical_artifacts row for artifact_id={artifact_id!r} "
                                "not found — artifact_persistence must complete successfully "
                                "before persist-visual-frames can run"
                            ),
                        )
                except HTTPException:
                    raise
                except Exception as _chk_exc:
                    raise HTTPException(
                        status_code=503,
                        detail=f"Preflight canonical_artifacts check failed: {_chk_exc}",
                    ) from _chk_exc

            # ── Phase 2: Persist raw frame rows ───────────────

            errors: list[str] = []
            frames_persisted = 0
            scenes_persisted = 0
            app_instances_persisted = 0
            controls_persisted = 0
            flow_edges_persisted = 0
            flows_persisted = 0

            # Normalize frames — ensure frame_id is stable
            normalized_frames: list[dict] = []
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                f = dict(frame)
                f.setdefault("frame_id", str(uuid.uuid4()))
                normalized_frames.append(f)

            try:
                async with engine.db_pool() as session:
                    for frame in normalized_frames:
                        frame_id = frame["frame_id"]
                        row_values = {
                            "frame_id": frame_id,
                            "tenant_id": tenant_id,
                            "session_id": session_id,
                            "job_id": frame.get("job_id", ""),
                            "video_id": frame.get("video_id") or None,
                            "frame_index": int(frame.get("frame_index", 0)),
                            "timestamp_seconds": float(frame.get("timestamp_seconds", 0.0)),
                            "frame_path": frame.get("frame_path", ""),
                            "frame_asset_path": frame.get("frame_asset_path", ""),
                            "artifact_id": artifact_id or None,
                            "application_type": frame.get("application_type", "unknown"),
                            "page_title": frame.get("page_title", ""),
                            "url_or_path": frame.get("url_or_path", ""),
                            "ui_elements_json": frame.get("ui_elements") or [],
                            "extracted_text": frame.get("extracted_text", ""),
                            "tables_json": frame.get("tables") or [],
                            "state_changes_json": frame.get("state_changes") or [],
                            "description": frame.get("description", ""),
                            "ocr_confidence": float(frame.get("ocr_confidence", 0.0)),
                            "is_keyframe": bool(frame.get("is_keyframe", False)),
                            "app_instance_id": None,
                            "scene_id": None,
                            "segmentation_confidence": 0.0,
                        }
                        stmt = (
                            pg_insert(VisualFrameRow)
                            .values(**row_values)
                            .on_conflict_do_update(
                                index_elements=["frame_id"],
                                set_={
                                    "frame_asset_path": row_values["frame_asset_path"],
                                    "artifact_id": row_values["artifact_id"],
                                    "application_type": row_values["application_type"],
                                    "page_title": row_values["page_title"],
                                    "url_or_path": row_values["url_or_path"],
                                    "ui_elements_json": row_values["ui_elements_json"],
                                    "extracted_text": row_values["extracted_text"],
                                    "tables_json": row_values["tables_json"],
                                    "state_changes_json": row_values["state_changes_json"],
                                    "description": row_values["description"],
                                    "ocr_confidence": row_values["ocr_confidence"],
                                    "is_keyframe": row_values["is_keyframe"],
                                },
                            )
                        )
                        await session.execute(stmt)
                        frames_persisted += 1
                    await session.commit()
            except Exception as exc:
                errors.append(f"Phase 2 frames: {exc}")
                return {
                    "success": False,
                    "frames_persisted": frames_persisted,
                    "scenes_persisted": 0,
                    "app_instances_persisted": 0,
                    "controls_persisted": 0,
                    "flow_edges_persisted": 0,
                    "flows_persisted": 0,
                    "artifact_id": artifact_id,
                    "errors": errors,
                }

            # ── Phase 2b: Refine application_type from OCR text ──
            # The eyes engine classifier can misclassify browser screens
            # as desktop_app/email_client when OCR quality is variable.
            # Apply lightweight browser-indicator check to correct obvious
            # misclassifications before scene building.
            _BROWSER_HINTS = ["ask gemini", "customize chrome", "new tab",
                              "com/", ".com", "http://", "https://", "www.",
                              "chrome", "firefox"]
            _NON_BROWSER_TYPES = {"desktop_app", "email_client", "unknown",
                                  "mainframe_3270", "terminal"}
            for frame in normalized_frames:
                current_type = (frame.get("application_type") or "").lower()
                if current_type in _NON_BROWSER_TYPES:
                    text_lower = (frame.get("extracted_text") or "").lower()
                    hits = sum(1 for hint in _BROWSER_HINTS if hint in text_lower)
                    if hits >= 2:
                        frame["application_type"] = "web_ui"

            # ── Phase 3: Build scenes + persist ───────────────

            from nexus_sdk.evidence.build_scenes import build_scenes as _build_scenes
            from nexus_sdk.evidence.app_segmenter import AppSegmenter as _AppSegmenter
            from nexus_sdk.evidence.control_extractor import ControlExtractor as _ControlExtractor
            from nexus_sdk.evidence.flow_builder import FlowBuilder as _FlowBuilder
            from nexus_sdk.evidence.flow_segmenter import FlowSegmenter as _FlowSegmenter, flow_groups_to_dicts as _flow_groups_to_dicts

            scenes: list[dict] = []
            try:
                scenes = _build_scenes(
                    frames=normalized_frames,
                    artifact_id=artifact_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                )
            except Exception as exc:
                errors.append(f"Phase 3 build_scenes: {exc}")

            if scenes:
                try:
                    async with engine.db_pool() as session:
                        # Clean up stale scenes from previous runs (e.g. if scene
                        # merging reduced the scene count, old high-index rows linger).
                        # Controls, flows, and edges are cleaned in their own phases.
                        new_scene_ids = {s["scene_id"] for s in scenes}
                        existing = await session.scalars(
                            sa_select(VisualSceneRow.scene_id).where(
                                VisualSceneRow.artifact_id == artifact_id,
                                VisualSceneRow.tenant_id == tenant_id,
                            )
                        )
                        stale_ids = [sid for sid in existing if sid not in new_scene_ids]
                        if stale_ids:
                            await session.execute(
                                sa_delete(VisualSceneRow).where(
                                    VisualSceneRow.scene_id.in_(stale_ids)
                                )
                            )

                        # Phase 2 (Wave A) guard fields are populated by the
                        # SDK build_scenes pipeline and consumed in-memory by
                        # the merge logic.  They are not (yet) backed by DB
                        # columns on visual_scenes, so strip them before insert
                        # to avoid SQLAlchemy 'Unconsumed column names' errors.
                        # If/when an Alembic migration adds these columns,
                        # remove this stripping and persist them straight
                        # through.
                        _scene_drop_keys = {
                            "frame_ids", "app_type",
                            "has_keyframe_boundary",
                            "visible_text_fingerprint",
                            "detected_controls",
                            "entry_action",
                            "exit_action",
                        }
                        for scene in scenes:
                            row_values = {
                                k: v for k, v in scene.items()
                                if k not in _scene_drop_keys
                            }
                            stmt = (
                                pg_insert(VisualSceneRow)
                                .values(**row_values)
                                .on_conflict_do_update(
                                    index_elements=["scene_id"],
                                    set_={
                                        "representative_frame_id": row_values["representative_frame_id"],
                                        "screen_name": row_values["screen_name"],
                                        "ocr_text": row_values["ocr_text"],
                                        "detected_url": row_values["detected_url"],
                                        "start_ms": row_values["start_ms"],
                                        "end_ms": row_values["end_ms"],
                                        "completeness_confidence": row_values["completeness_confidence"],
                                        # Phase 8 — structured summaries and quality
                                        "scene_state_summary": row_values.get("scene_state_summary"),
                                        "duration_ms": row_values.get("duration_ms"),
                                        "duration_quality": row_values.get("duration_quality", "unknown"),
                                        "scene_quality": row_values.get("scene_quality", "weak"),
                                    },
                                )
                            )
                            await session.execute(stmt)
                            scenes_persisted += 1

                        # Back-fill visual_frames.scene_id
                        for scene in scenes:
                            scene_id = scene["scene_id"]
                            for fid in scene.get("frame_ids", []):
                                if fid:
                                    await session.execute(
                                        sa_update(VisualFrameRow)
                                        .where(VisualFrameRow.frame_id == fid)
                                        .values(scene_id=scene_id)
                                    )
                        # Reconcile canonical_artifacts.scene_count with the
                        # real number of persisted scenes. The earlier
                        # persist-canonical-artifact step seeds it from
                        # visual_analysis.total_frames_analyzed (= frame count
                        # in the new eyes pipeline), which is wrong once
                        # build_scenes() collapses frames into scenes.
                        await session.execute(
                            sa_update(CanonicalArtifactRow)
                            .where(CanonicalArtifactRow.artifact_id == artifact_id)
                            .values(scene_count=len(scenes))
                        )
                        await session.commit()
                except Exception as exc:
                    errors.append(f"Phase 3 persist: {exc}")

            # ── Phase 5: App segmentation + persist ───────────

            enriched_scenes: list[dict] = scenes
            app_instances: list[dict] = []
            try:
                segmenter = _AppSegmenter()
                enriched_scenes, app_instances = segmenter.segment(
                    scenes=scenes,
                    artifact_id=artifact_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                )
            except Exception as exc:
                errors.append(f"Phase 5 segment: {exc}")

            if app_instances:
                try:
                    async with engine.db_pool() as db_s:
                        # Clean old app instances for this artifact (idempotent re-processing)
                        await db_s.execute(
                            sa_delete(AppInstanceRow)
                            .where(
                                AppInstanceRow.artifact_id == artifact_id,
                                AppInstanceRow.tenant_id == tenant_id,
                            )
                        )
                        for inst in app_instances:
                            stmt = (
                                pg_insert(AppInstanceRow)
                                .values(**inst)
                                .on_conflict_do_update(
                                    index_elements=["instance_id"],
                                    set_={
                                        "app_name": inst.get("app_name", ""),
                                        "app_type": inst.get("app_type", "unknown"),
                                    },
                                )
                            )
                            await db_s.execute(stmt)
                            app_instances_persisted += 1

                        # Back-fill visual_scenes.app_instance_id
                        for scene in enriched_scenes:
                            app_id = scene.get("app_instance_id")
                            scene_id = scene.get("scene_id")
                            seg_confidence = float(scene.get("segmentation_confidence", 0.0))
                            if scene_id and app_id:
                                await db_s.execute(
                                    sa_update(VisualSceneRow)
                                    .where(VisualSceneRow.scene_id == scene_id)
                                    .values(app_instance_id=app_id)
                                )
                                # Also update visual_frames with app_instance_id + segmentation_confidence
                                await db_s.execute(
                                    sa_update(VisualFrameRow)
                                    .where(VisualFrameRow.scene_id == scene_id)
                                    .values(
                                        app_instance_id=app_id,
                                        segmentation_confidence=seg_confidence,
                                    )
                                )
                        await db_s.commit()
                except Exception as exc:
                    errors.append(f"Phase 5 persist: {exc}")

            # ── Phase 6: Extract controls + persist ───────────

            # Build frame lookup by frame_id for control extraction
            frame_by_id: dict[str, dict] = {f["frame_id"]: f for f in normalized_frames}
            # Back-propagate scene_id onto the in-memory frame dicts so Phase 7
            # action enrichment (FlowBuilder.enrich_with_actions) can group frames
            # by scene.  Without this, frames_by_scene inside FlowBuilder is empty
            # and verb detection always fails, producing generic "Reviewed…" /
            # "Navigated to…" labels instead of "Clicked Submit" / "Entered '5000'
            # in Annual Premium".  This is a critical correctness fix: Phase 7
            # signals were dead until now.
            for scene in enriched_scenes:
                sid = scene.get("scene_id", "")
                if not sid:
                    continue
                for fid in scene.get("frame_ids", []) or []:
                    f = frame_by_id.get(fid)
                    if f is not None:
                        f["scene_id"] = sid
            # Build per-scene frame lists using the now-populated scene_id.
            frames_by_scene: dict[str, list[dict]] = {
                scene["scene_id"]: [
                    frame_by_id[fid]
                    for fid in scene.get("frame_ids", [])
                    if fid and fid in frame_by_id
                ]
                for scene in enriched_scenes
                if scene.get("scene_id")
            }
            all_controls: list[dict] = []
            extractor = _ControlExtractor()

            for scene in enriched_scenes:
                rep_fid = scene.get("representative_frame_id")
                rep_frame = frame_by_id.get(rep_fid, {}) if rep_fid else {}
                scene_frames = frames_by_scene.get(scene.get("scene_id", ""), [])
                if rep_frame:
                    try:
                        ctrls = extractor.extract(
                            scene=scene,
                            frame=rep_frame,
                            artifact_id=artifact_id,
                            tenant_id=tenant_id,
                            all_frames=scene_frames,
                        )
                        all_controls.extend(ctrls)
                    except Exception as exc:
                        errors.append(f"Phase 6 extract scene {scene.get('scene_id')}: {exc}")

            if all_controls:
                try:
                    async with engine.db_pool() as db_c:
                        # Swap-safe: delete stale rows only after the new extraction
                        # has produced results.  This prevents a reprocessing failure
                        # from silently downgrading an artifact to zero controls.
                        if artifact_id:
                            from sqlalchemy import delete as sa_delete_ctrl
                            from nexus_sdk.db.models import EvidenceControlRow as _ECRow
                            await db_c.execute(
                                sa_delete_ctrl(_ECRow).where(_ECRow.artifact_id == artifact_id)
                            )
                        for ctrl in all_controls:
                            stmt = (
                                pg_insert(EvidenceControlRow)
                                .values(**ctrl)
                                .on_conflict_do_nothing()
                            )
                            await db_c.execute(stmt)
                            controls_persisted += 1
                        await db_c.commit()
                except Exception as exc:
                    errors.append(f"Phase 6 persist: {exc}")

                # ── Phase 6b: UI dictionary registration ──────
                # Push every freshly-extracted control into the
                # tenant-scoped UI dictionary so the next recording on
                # the same page inherits the selector and confidence.
                # Non-fatal — dictionary failures must never block the
                # main artifact pipeline.
                try:
                    from nexus_sdk.dictionary import UIDictionary
                    from nexus_sdk.dictionary.registry import DictionaryRecognition
                    # Index scenes for page_key + domain lookup.
                    scene_index_by_id = {
                        s.get("scene_id"): s for s in enriched_scenes
                    }
                    recognitions: list[DictionaryRecognition] = []
                    for ctrl in all_controls:
                        scene = scene_index_by_id.get(ctrl.get("scene_id")) or {}
                        summary = scene.get("scene_state_summary") or {}
                        page_key = summary.get("page_key") or ""
                        domain = summary.get("domain") or ""
                        # Skip controls without a meaningful identity to
                        # avoid polluting the dictionary with unlabelled
                        # vision-only detections.
                        label = (ctrl.get("label_text") or "").strip()
                        if not label:
                            continue
                        bbox = ctrl.get("bounding_box") or {}
                        cx = cy = None
                        if isinstance(bbox, dict):
                            if all(k in bbox for k in ("x", "y", "width", "height")):
                                cx = int(bbox["x"]) + int(bbox["width"]) // 2
                                cy = int(bbox["y"]) + int(bbox["height"]) // 2
                        recognitions.append(DictionaryRecognition(
                            page_key=page_key,
                            domain=domain,
                            element_type=ctrl.get("element_type") or "",
                            label_text=label,
                            display_label=ctrl.get("display_label") or "",
                            action_kind=ctrl.get("action_kind") or "",
                            preferred_selector=ctrl.get("playwright_selector") or "",
                            selector_confidence=float(ctrl.get("selector_confidence") or 0.0),
                            selector_source=ctrl.get("selector_source") or "unknown",
                            bbox_centre_x=cx,
                            bbox_centre_y=cy,
                            metadata={
                                "automation_ready": bool(ctrl.get("automation_ready")),
                                "first_seen_artifact": artifact_id,
                            },
                        ))
                    if recognitions:
                        async with engine.db_pool() as dict_session:
                            from sqlalchemy import text as sa_text
                            if tenant_id:
                                await dict_session.execute(
                                    sa_text(
                                        "SELECT set_config('nexus.current_tenant_id', :tid, true)"
                                    ),
                                    {"tid": tenant_id},
                                )
                            ui_dict = UIDictionary(dict_session, tenant_id=tenant_id)
                            await ui_dict.record_recognitions(recognitions)
                            await dict_session.commit()
                        import logging
                        logging.getLogger("spine").info(
                            "spine.ui_dictionary.registered tenant=%s artifact=%s entries=%d",
                            tenant_id, artifact_id, len(recognitions),
                        )
                except Exception as exc:
                    errors.append(f"Phase 6b UI dictionary: {exc}")

            # ── Phase 5b: Flow segmentation + persist ─────────

            flow_groups: list[dict] = []
            try:
                _flow_seg = _FlowSegmenter()
                _flow_objs = _flow_seg.segment(
                    scenes=enriched_scenes,
                    artifact_id=artifact_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    controls=all_controls,
                )
                flow_groups = _flow_groups_to_dicts(_flow_objs)
            except Exception as exc:
                errors.append(f"Phase 5b flow_segment: {exc}")

            if flow_groups:
                # Pre-build scene→flow_id mapping before popping scene_ids
                _scene_to_flow: dict[str, str] = {}
                for fg in flow_groups:
                    fid = fg["flow_id"]
                    for sid in fg.get("scene_ids", []):
                        _scene_to_flow[sid] = fid

                try:
                    async with engine.db_pool() as db_fg:
                        # Clean old flows for this artifact (idempotent re-processing)
                        await db_fg.execute(
                            sa_delete(VisualFlowRow)
                            .where(
                                VisualFlowRow.artifact_id == artifact_id,
                                VisualFlowRow.tenant_id == tenant_id,
                            )
                        )
                        for fg in flow_groups:
                            scene_ids_in_flow = fg.pop("scene_ids", [])
                            stmt = (
                                pg_insert(VisualFlowRow)
                                .values(**fg)
                                .on_conflict_do_update(
                                    index_elements=["flow_id"],
                                    set_={
                                        "flow_label": fg["flow_label"],
                                        "scene_count": fg["scene_count"],
                                    },
                                )
                            )
                            await db_fg.execute(stmt)
                            flows_persisted += 1

                            # Back-fill visual_scenes.flow_id
                            flow_id = fg["flow_id"]
                            for sid in scene_ids_in_flow:
                                if sid:
                                    await db_fg.execute(
                                        sa_update(VisualSceneRow)
                                        .where(VisualSceneRow.scene_id == sid)
                                        .values(flow_id=flow_id)
                                    )
                        await db_fg.commit()
                except Exception as exc:
                    errors.append(f"Phase 5b persist: {exc}")
            else:
                _scene_to_flow = {}

            # Back-fill flow_id onto in-memory scenes so Phase 7 can
            # skip cross-flow edges.
            if _scene_to_flow:
                for sc in enriched_scenes:
                    sid = sc.get("scene_id", "")
                    if sid in _scene_to_flow:
                        sc["flow_id"] = _scene_to_flow[sid]

            # ── Phase 7: Build flow graph + persist ───────────

            flow_edges: list[dict] = []
            try:
                builder = _FlowBuilder()
                layer1 = builder.build_observed_transitions(
                    scenes=enriched_scenes,
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                )
                flow_edges = builder.enrich_with_actions(
                    edges=layer1,
                    frames=normalized_frames,
                    controls=all_controls,
                    scenes=enriched_scenes,
                    scene_transitions=scene_transitions,
                )
            except Exception as exc:
                errors.append(f"Phase 7 build: {exc}")

            if flow_edges:
                try:
                    async with engine.db_pool() as db_f:
                        # Clean stale edges before re-inserting (idempotent replay)
                        await db_f.execute(
                            sa_delete(VisualFlowEdgeRow).where(
                                VisualFlowEdgeRow.artifact_id == artifact_id,
                                VisualFlowEdgeRow.tenant_id == tenant_id,
                            )
                        )
                        for edge in flow_edges:
                            # Attach flow_id from source scene (guaranteed
                            # same as dest because cross-flow edges are now
                            # skipped in build_observed_transitions).
                            src_sid = edge.get("from_scene_id", "")
                            edge.setdefault("flow_id", _scene_to_flow.get(src_sid))
                            stmt = (
                                pg_insert(VisualFlowEdgeRow)
                                .values(**edge)
                                .on_conflict_do_update(
                                    index_elements=["edge_id"],
                                    set_={
                                        "edge_type": edge.get("edge_type", "transition"),
                                        "action_type": edge.get("action_type"),
                                        "action_value": edge.get("action_value"),
                                        "evidence_confidence": edge.get("evidence_confidence", 0.0),
                                        # Phase 8 — structured action summary and quality
                                        "primary_action_summary": edge.get("primary_action_summary"),
                                        "action_quality": edge.get("action_quality", "weak"),
                                        "action_confidence": edge.get("action_confidence", 0.0),
                                    },
                                )
                            )
                            await db_f.execute(stmt)
                            flow_edges_persisted += 1
                        await db_f.commit()
                except Exception as exc:
                    errors.append(f"Phase 7 persist: {exc}")

            # ── Phase 7b (Phase 2 / A1 back-fill) ──────────────────
            # Back-propagate detected_controls / entry_action / exit_action
            # onto enriched_scenes so any subsequent in-process consumer of
            # the scene list (diff harness, replay, future re-merge) sees the
            # full guard signal set required by `_can_merge_phase2`.
            #
            # This is a pure in-memory mutation — no DB persistence yet.
            # When EYES_PHASE2_GUARDS is OFF the data is harmless extra fields
            # that downstream consumers ignore.  When ON, a re-call to
            # `build_scenes()` on the same artifact gets the full guards.
            if os.environ.get("EYES_PHASE2_GUARDS", "false").strip().lower() in (
                "1", "true", "yes", "on",
            ):
                try:
                    # Index controls by scene_id for O(1) lookup.
                    ctrls_by_scene: dict[str, list[str]] = {}
                    for c in all_controls or []:
                        sid = c.get("scene_id", "")
                        label = (c.get("label_text") or "").strip()
                        if sid and label:
                            ctrls_by_scene.setdefault(sid, []).append(label)

                    # Index edges by from_scene / to_scene → action_kind.
                    entry_by_scene: dict[str, str] = {}
                    exit_by_scene: dict[str, str] = {}
                    for e in flow_edges or []:
                        summary = e.get("primary_action_summary") or {}
                        kind = (summary.get("action_kind") or e.get("action_type") or "").strip()
                        if not kind:
                            continue
                        from_sid = e.get("from_scene_id", "")
                        to_sid = e.get("to_scene_id", "")
                        if from_sid:
                            exit_by_scene[from_sid] = kind
                        if to_sid:
                            entry_by_scene[to_sid] = kind

                    for sc in enriched_scenes:
                        sid = sc.get("scene_id", "")
                        if not sid:
                            continue
                        # detected_controls — sorted, deduplicated label list.
                        # Sorted so guard equality (set comparison) is stable.
                        sc["detected_controls"] = sorted(set(ctrls_by_scene.get(sid, [])))
                        sc["entry_action"] = entry_by_scene.get(sid, "")
                        sc["exit_action"] = exit_by_scene.get(sid, "")
                except Exception as exc:
                    # Non-fatal — this is metadata back-fill, not a correctness path.
                    errors.append(f"Phase 7b guard back-fill: {exc}")

            # Expose per-scene quality flags so the canonical quality gate
            # can weigh the eyes engine's own per-scene assessment instead
            # of scoring purely on scene count.
            scene_qualities = [
                str(s.get("scene_quality") or "weak") for s in (scenes or [])
            ]
            scene_descriptions = [
                str(s.get("screen_name") or "") for s in (scenes or [])
            ]

            return {
                "success": len(errors) == 0,
                "frames_persisted": frames_persisted,
                "scenes_persisted": scenes_persisted,
                "app_instances_persisted": app_instances_persisted,
                "controls_persisted": controls_persisted,
                "flow_edges_persisted": flow_edges_persisted,
                "flows_persisted": flows_persisted,
                "scene_qualities": scene_qualities,
                "scene_descriptions": scene_descriptions,
                "artifact_id": artifact_id,
                "errors": errors,
            }

        # ── Persist Action Evidence (cursor + intent + triangulation) ──

        @app.post("/api/v1/spine/persist-action-evidence")
        async def persist_action_evidence(
            request: dict = None,
            user: NexusUser = Depends(get_current_user),
        ):
            """Run the multimodal action-evidence pipeline against a persisted
            canonical artifact.

            This stage runs after ``/persist-visual-frames`` has populated
            visual_scenes / visual_frames / evidence_controls.  It then:

              1. Loads visual_frames (with frame_path) and visual_scenes
                 (with scene_index → frames assignment) from PostgreSQL
                 for the artifact.
              2. Runs :class:`nexus_sdk.cursor.CursorTracker` over the
                 ordered frame sequence and persists cursor_events rows.
              3. Loads transcript_segments for the session and runs
                 :func:`nexus_sdk.audio.extract_intent_events` to surface
                 SME-narration intent timestamps.
              4. For each scene with ≥2 frames, runs
                 :class:`nexus_sdk.evidence.triangulator.TriangulatedClassifier`
                 against the four signals (OCR diff + cursor + LLaVA delta
                 + audio intent) and produces evidence_steps rows.
              5. Bulk-inserts cursor_events and evidence_steps in two
                 transactions so a per-row failure cannot strand the whole
                 batch.

            Failure is non-fatal at the stage level — an empty result with
            a populated ``errors`` list lets the canonical pipeline mark
            the artifact complete and downstream consumers degrade
            gracefully (the legacy frame_actions JSON in
            scene_state_summary remains a fallback).
            """
            if request is None:
                request = {}

            tenant_id = request.get("tenant_id", "")
            session_id = request.get("session_id", "")
            artifact_id = request.get("artifact_id", "")
            raw_transcript = request.get("raw_transcript") or {}

            errors: list[str] = []
            cursor_events_persisted = 0
            evidence_steps_persisted = 0

            if not artifact_id:
                return {
                    "success": False,
                    "cursor_events_persisted": 0,
                    "evidence_steps_persisted": 0,
                    "artifact_id": artifact_id,
                    "errors": ["artifact_id required"],
                }
            if not hasattr(engine, "db_pool") or not engine.db_pool:
                return {
                    "success": False,
                    "cursor_events_persisted": 0,
                    "evidence_steps_persisted": 0,
                    "artifact_id": artifact_id,
                    "errors": ["database not available"],
                }

            from nexus_sdk.db.models import (
                VisualFrameRow, VisualSceneRow, EvidenceControlRow,
                EvidenceStepRow, CursorEventRow, TranscriptSegmentRow,
            )
            from sqlalchemy import select as sa_select, text as sa_text
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from nexus_sdk.audio import extract_intent_events
            from nexus_sdk.evidence.step_persistence import (
                cursor_events_to_db_rows,
                triangulated_actions_to_step_rows,
            )
            from nexus_sdk.evidence.triangulator import TriangulatedClassifier

            # ── Step 1: load frames + scenes + controls from DB ───
            frames_by_id: dict[str, dict] = {}
            scenes_rows: list[dict] = []
            frame_id_lookup: dict[int, str] = {}
            controls_by_scene: dict[str, list[dict]] = {}
            try:
                async with engine.db_pool() as load_session:
                    if tenant_id:
                        await load_session.execute(
                            sa_text(
                                "SELECT set_config('nexus.current_tenant_id', :tid, true)"
                            ),
                            {"tid": tenant_id},
                        )
                    rows = await load_session.execute(
                        sa_select(VisualFrameRow).where(
                            VisualFrameRow.artifact_id == artifact_id
                        ).order_by(VisualFrameRow.frame_index)
                    )
                    for r in rows.scalars().all():
                        frames_by_id[r.frame_id] = {
                            "frame_id": r.frame_id,
                            "frame_index": int(r.frame_index or 0),
                            "frame_path": r.frame_path or "",
                            "scene_id": r.scene_id,
                            "timestamp_seconds": float(r.timestamp_seconds or 0.0),
                            "extracted_text": r.extracted_text or "",
                            "description": r.description or "",
                            "ocr_confidence": float(r.ocr_confidence or 0.0),
                            "ui_elements": r.ui_elements_json or [],
                        }
                        frame_id_lookup[int(r.frame_index or 0)] = r.frame_id

                    s_rows = await load_session.execute(
                        sa_select(VisualSceneRow).where(
                            VisualSceneRow.artifact_id == artifact_id
                        ).order_by(VisualSceneRow.scene_index)
                    )
                    for s in s_rows.scalars().all():
                        scenes_rows.append({
                            "scene_id": s.scene_id,
                            "scene_index": int(s.scene_index or 0),
                            "screen_name": s.screen_name or "",
                            "scene_state_summary": s.scene_state_summary or {},
                            "start_ms": int(s.start_ms or 0),
                            "end_ms": int(s.end_ms or 0),
                            # ocr_text feeds the pattern-based form-value
                            # extractor that synthesises evidence_steps
                            # when value_text on the control is empty.
                            "ocr_text": s.ocr_text or "",
                        })

                    c_rows = await load_session.execute(
                        sa_select(EvidenceControlRow).where(
                            EvidenceControlRow.artifact_id == artifact_id
                        )
                    )
                    for c in c_rows.scalars().all():
                        controls_by_scene.setdefault(c.scene_id, []).append({
                            "control_id": c.control_id,
                            "label_text": c.label_text or "",
                            "element_type": c.element_type or "",
                            # value_text / observed_value drive the
                            # "filled-form" synthetic-step pass below: a
                            # control with a non-empty value is direct
                            # evidence of a user action (the field shows
                            # a value because they typed/selected it).
                            "value_text": c.value_text or "",
                            "observed_value": c.observed_value or "",
                            "action_kind": c.action_kind or "",
                            "frame_id": c.frame_id or "",
                            "automation_ready": bool(c.automation_ready),
                        })
            except Exception as exc:
                errors.append(f"load: {exc}")
                return {
                    "success": False,
                    "cursor_events_persisted": 0,
                    "evidence_steps_persisted": 0,
                    "artifact_id": artifact_id,
                    "errors": errors,
                }

            if not frames_by_id or not scenes_rows:
                return {
                    "success": True,
                    "cursor_events_persisted": 0,
                    "evidence_steps_persisted": 0,
                    "artifact_id": artifact_id,
                    "errors": errors,
                    "message": "no frames or scenes — nothing to triangulate",
                }

            # ── Step 2: cursor tracking ───────────────────────────
            cursor_events: list[dict] = []
            try:
                from nexus_sdk.cursor import CursorTracker, FrameInput
                ordered = sorted(
                    frames_by_id.values(), key=lambda f: f["frame_index"],
                )
                tracker_inputs: list[FrameInput] = []
                for f in ordered:
                    if not f["frame_path"]:
                        continue
                    if not os.path.exists(f["frame_path"]):
                        continue
                    tracker_inputs.append(FrameInput(
                        frame_id=f["frame_id"],
                        frame_index=f["frame_index"],
                        timestamp_ms=int(round(f["timestamp_seconds"] * 1000.0)),
                        frame_path=f["frame_path"],
                    ))
                if len(tracker_inputs) >= 2:
                    tracker = CursorTracker()
                    raw_events = tracker.track_frames(tracker_inputs)
                    cursor_events = cursor_events_to_db_rows(
                        events=raw_events,
                        artifact_id=artifact_id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        frame_id_lookup=frame_id_lookup,
                    )
            except ImportError:
                # OpenCV / numpy not installed in this environment — cursor
                # signal stays absent but the rest of the pipeline runs.
                errors.append("cursor: opencv/numpy unavailable")
            except Exception as exc:
                errors.append(f"cursor: {exc}")

            # ── Step 3: audio intent extraction ───────────────────
            audio_intents: list[dict] = []
            try:
                # Prefer transcript_segments rows from the DB (canonical store).
                # Fall back to raw_transcript['segments'] when DB rows are
                # absent (e.g. audio_transcription completed but rows
                # haven't yet propagated due to async lag).
                segments_input: list[dict] = []
                async with engine.db_pool() as load_session:
                    if tenant_id:
                        await load_session.execute(
                            sa_text(
                                "SELECT set_config('nexus.current_tenant_id', :tid, true)"
                            ),
                            {"tid": tenant_id},
                        )
                    seg_rows = await load_session.execute(
                        sa_select(TranscriptSegmentRow).where(
                            TranscriptSegmentRow.session_id == session_id
                        ).order_by(TranscriptSegmentRow.segment_index)
                    )
                    for seg in seg_rows.scalars().all():
                        segments_input.append({
                            "text": seg.text or "",
                            "start_time": float(seg.start_time or 0.0),
                            "end_time": float(seg.end_time or 0.0),
                            "confidence": float(seg.confidence or 1.0),
                            "speaker": seg.speaker or "",
                            "segment_index": int(seg.segment_index or 0),
                            # words_json carries Whisper per-word timestamps;
                            # the intent extractor uses them to anchor each
                            # event at the verb's spoken time instead of the
                            # segment start, which is the difference between
                            # "1 audio anchor for a 20s segment" and "one
                            # anchor per narrated action".
                            "words_json": list(seg.words_json or []),
                        })
                if not segments_input:
                    segments_input = list(raw_transcript.get("segments") or [])
                if segments_input:
                    intents = extract_intent_events(segments_input)
                    audio_intents = [
                        {
                            "timestamp_ms": int(i.timestamp_ms),
                            "intent_kind": i.intent_kind,
                            "target_phrase": i.target_phrase,
                            "raw_text": i.raw_text,
                            "confidence": float(i.confidence),
                        }
                        for i in intents
                    ]
            except Exception as exc:
                errors.append(f"audio_intents: {exc}")

            # ── Step 4: triangulate per scene ─────────────────────
            classifier = TriangulatedClassifier()
            evidence_step_rows: list[dict] = []
            try:
                # Group frames by scene_id for triangulation.
                frames_by_scene: dict[str, list[dict]] = {}
                for f in frames_by_id.values():
                    sid = f.get("scene_id")
                    if sid:
                        frames_by_scene.setdefault(sid, []).append(f)

                # Cursor events keyed by scene via frame_id ↔ scene_id.
                cursor_by_scene: dict[str, list[dict]] = {}
                for ce in cursor_events:
                    fid = ce.get("frame_id")
                    if not fid:
                        continue
                    sid = (frames_by_id.get(fid) or {}).get("scene_id")
                    if sid:
                        cursor_by_scene.setdefault(sid, []).append(ce)

                for scene in scenes_rows:
                    scene_id = scene["scene_id"]
                    sframes = sorted(
                        frames_by_scene.get(scene_id, []),
                        key=lambda f: f["frame_index"],
                    )
                    if len(sframes) < 2:
                        continue
                    # Only intents whose timestamp falls inside the scene
                    # window — the triangulator's own audio_align_ms tolerance
                    # extends this slightly.
                    scene_intents = [
                        i for i in audio_intents
                        if scene["start_ms"] - classifier.config.audio_align_ms
                        <= i["timestamp_ms"]
                        <= scene["end_ms"] + classifier.config.audio_align_ms
                    ]
                    actions = classifier.classify_actions_in_scene(
                        scene,
                        scene_frames=sframes,
                        cursor_events=cursor_by_scene.get(scene_id, []),
                        audio_intents=scene_intents,
                        controls=controls_by_scene.get(scene_id, []),
                    )
                    if actions:
                        evidence_step_rows.extend(
                            triangulated_actions_to_step_rows(
                                actions=actions,
                                artifact_id=artifact_id,
                                scene_id=scene_id,
                                tenant_id=tenant_id,
                                session_id=session_id,
                            )
                        )

                # ── Synthetic-step generation ──────────────────────
                # The triangulator only fires on intra-scene frame-pair
                # diffs. Two structural categories of real visual evidence
                # never reach it:
                #   (a) FILLED CONTROLS — eyes captured a control with a
                #       non-empty value_text. The field showing a value IS
                #       proof of a select/enter action. Single-frame
                #       scenes (no frame pair to diff) lose this entirely.
                #   (b) OUTGOING TRANSITIONS — when scene S transitions to
                #       scene T via a confirmed flow edge, that transition
                #       captures a user action (click Continue, navigate,
                #       etc.) that lives between scenes, not inside them.
                # Both are derived from data the pipeline already has —
                # no fabrication, no audio.
                existing_keys: set[tuple[str, str]] = {
                    (r["scene_id"], r["trigger_control_id"] or "")
                    for r in evidence_step_rows
                }
                # (a) Eyes already extracted value_text on the control.
                synth_rows = _synthesize_steps_from_filled_controls(
                    scenes_rows=scenes_rows,
                    controls_by_scene=controls_by_scene,
                    frames_by_id=frames_by_id,
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    existing_keys=existing_keys,
                )
                evidence_step_rows.extend(synth_rows)
                # (b) Eyes couldn't extract value_text but the rendered
                # value is still in the scene's full OCR dump. Pattern-
                # match the value next to the control's label. This is
                # the workhorse for Zoom-compressed recordings where
                # per-control OCR fails but page-level OCR still catches
                # the selected text somewhere on screen.
                ocr_synth_rows = _synthesize_steps_from_ocr_form_match(
                    scenes_rows=scenes_rows,
                    controls_by_scene=controls_by_scene,
                    frames_by_id=frames_by_id,
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    existing_keys=existing_keys,
                )
                evidence_step_rows.extend(ocr_synth_rows)
            except Exception as exc:
                errors.append(f"triangulator: {exc}")

            # Outgoing-edge synthetic steps (runs even if triangulator
            # raised — these are independent of the per-pair classifier).
            try:
                edge_rows = await _load_visual_flow_edges_for_artifact(
                    engine, tenant_id, artifact_id,
                )
                edge_synth = _synthesize_steps_from_outgoing_edges(
                    scenes_rows=scenes_rows,
                    edges=edge_rows,
                    frames_by_id=frames_by_id,
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    existing_step_rows=evidence_step_rows,
                )
                evidence_step_rows.extend(edge_synth)
            except Exception as exc:
                errors.append(f"edge_synth: {exc}")

            # ── Final quality gate ────────────────────────────────────
            # One last pass over every row regardless of source. Filters
            # triangulator-emitted rows that the SDK couldn't reject (e.g.
            # multi-signal rows where both target and value are OCR junk).
            # Keeps the bottom panel honest: every persisted row passes a
            # structural plausibility check on its target/value pair.
            evidence_step_rows = [
                r for r in evidence_step_rows
                if _is_meaningful_step(
                    r.get("target_label", ""),
                    r.get("observed_value", ""),
                )
            ]
            # PII defence-in-depth: structured PII patterns (SSN, phone,
            # email, etc.) that the meaningful-step filter might let pass
            # are scrubbed here before any row hits the database. Same
            # _redact_pii_in_text used on transcript / visual_summary.
            for r in evidence_step_rows:
                r["target_label"] = _redact_pii_in_text(r.get("target_label", ""))
                r["observed_value"] = _redact_pii_in_text(r.get("observed_value", ""))
                if r.get("audio_intent_text"):
                    r["audio_intent_text"] = _redact_pii_in_text(r["audio_intent_text"])
            # Dedupe across synthesizers: the same on-screen state change
            # often produces a triangulator row AND a synthesizer row
            # ("Gender = Male" from both ocr_form_match and a filled_control,
            # or from the triangulator's ocr_control_match path). Keep the
            # single highest-confidence row per (scene, target, value).
            # Rows with no target AND no value are preserved unchanged
            # (rare and not part of the duplicate class we're killing).
            def _dedup_key(r: dict) -> tuple[str, str, str]:
                return (
                    r.get("scene_id", ""),
                    (r.get("target_label") or "").strip().lower(),
                    (r.get("observed_value") or "").strip().lower(),
                )
            best_by_key: dict[tuple[str, str, str], dict] = {}
            preserved_zero: list[dict] = []
            for r in evidence_step_rows:
                k = _dedup_key(r)
                if not k[1] and not k[2]:
                    preserved_zero.append(r)
                    continue
                cur = best_by_key.get(k)
                if cur is None or float(r.get("confidence", 0)) > float(cur.get("confidence", 0)):
                    best_by_key[k] = r
            evidence_step_rows = list(best_by_key.values()) + preserved_zero
            try:
                # Re-number step_index per scene now that some rows were
                # filtered/deduped — keeps the UI ordering contiguous (no
                # 0, 2, 4 gaps). Ordering: primarily by start_ms (so the
                # panel reads chronologically — what happened first), with
                # confidence descending as the tie-breaker so when two
                # steps share a timestamp the higher-confidence one comes
                # first and the weaker one sorts below it.
                _by_scene: dict[str, list[dict]] = {}
                for r in evidence_step_rows:
                    _by_scene.setdefault(r["scene_id"], []).append(r)
                for _sid, _rows in _by_scene.items():
                    _rows.sort(key=lambda r: (
                        int(r.get("start_ms", 0)),
                        -float(r.get("confidence", 0.0)),
                        int(r.get("step_index", 0)),
                    ))
                    for new_idx, r in enumerate(_rows):
                        r["step_index"] = new_idx
            except Exception as exc:
                errors.append(f"step_renumber: {exc}")

            # ── Step 5: bulk-insert cursor_events and evidence_steps ──
            try:
                async with engine.db_pool() as write_session:
                    if tenant_id:
                        await write_session.execute(
                            sa_text(
                                "SELECT set_config('nexus.current_tenant_id', :tid, true)"
                            ),
                            {"tid": tenant_id},
                        )
                    if cursor_events:
                        ce_stmt = (
                            pg_insert(CursorEventRow)
                            .values(cursor_events)
                            .on_conflict_do_nothing(index_elements=["event_id"])
                        )
                        await write_session.execute(ce_stmt)
                        cursor_events_persisted = len(cursor_events)
                    if evidence_step_rows:
                        es_stmt = (
                            pg_insert(EvidenceStepRow)
                            .values(evidence_step_rows)
                            .on_conflict_do_nothing(index_elements=["step_id"])
                        )
                        await write_session.execute(es_stmt)
                        evidence_steps_persisted = len(evidence_step_rows)
                    await write_session.commit()
            except Exception as exc:
                errors.append(f"persist: {exc}")

            # ── Step 6: emit canonical-pipeline quality metrics ──
            # Aggregate per-step / per-scene / per-signal numbers and
            # emit Prometheus samples + drift-detector observations so
            # operators get visibility the moment quality regresses.
            # Non-fatal — metrics emission must never block the data
            # path.
            try:
                from nexus_sdk.observability.canonical_metrics import (
                    CanonicalQualityRecorder,
                    build_quality_summary,
                )
                # Load the freshly-persisted controls so the summary
                # reflects the current run, not stale rows.
                async with engine.db_pool() as load_q:
                    if tenant_id:
                        await load_q.execute(
                            sa_text(
                                "SELECT set_config('nexus.current_tenant_id', :tid, true)"
                            ),
                            {"tid": tenant_id},
                        )
                    from sqlalchemy import select as _select
                    from nexus_sdk.db.models import EvidenceControlRow as _EC
                    crows = await load_q.execute(
                        _select(_EC).where(_EC.artifact_id == artifact_id)
                    )
                    controls_for_metrics = [
                        {
                            "automation_ready": bool(c.automation_ready),
                            "label_text": c.label_text or "",
                            "element_type": c.element_type or "",
                        }
                        for c in crows.scalars().all()
                    ]

                duration_seconds = 0.0
                if scenes_rows:
                    duration_seconds = max(
                        0.0,
                        max((s["end_ms"] for s in scenes_rows), default=0) / 1000.0,
                    )

                quality = build_quality_summary(
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    duration_seconds=duration_seconds,
                    scenes=scenes_rows,
                    evidence_steps=evidence_step_rows,
                    cursor_events=cursor_events,
                    audio_intents=audio_intents,
                    controls=controls_for_metrics,
                    status="completed" if not errors else "partial",
                )
                CanonicalQualityRecorder().record(quality)
            except Exception as exc:
                # Emission failures are observability-only; record but
                # never fail the response — operators see the absence
                # of metrics in their dashboard, not a broken artifact.
                errors.append(f"metrics: {exc}")

            return {
                "success": len(errors) == 0,
                "cursor_events_persisted": cursor_events_persisted,
                "evidence_steps_persisted": evidence_steps_persisted,
                "audio_intents_detected": len(audio_intents),
                "scenes_processed": len(scenes_rows),
                "artifact_id": artifact_id,
                "errors": errors,
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
            valid_statuses = {
                "pending", "processing", "minimal", "persisted",
                "completed", "completed_degraded", "failed", "needs_review",
            }
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


def _capture_run_provenance_safe(
    *,
    artifact_id: str,
    chain_id: str,
    processing_profile: str = "",
    engine_versions: Optional[dict[str, str]] = None,
    model_resolved: Optional[dict[str, str]] = None,
) -> dict:
    """Capture run provenance, swallowing any error.

    Reproducibility data is best-effort — if the recorder fails (missing
    package, broken env), we want the artifact to still persist with at
    least a partial record rather than the whole canonical_artifacts row
    failing to write.
    """
    try:
        from nexus_sdk.provenance import capture_run_provenance
        prov = capture_run_provenance(
            artifact_id=artifact_id,
            chain_id=chain_id,
            processing_profile=processing_profile,
            engine_versions=engine_versions or {},
            model_resolved=model_resolved or {},
        )
        return prov.to_dict()
    except Exception as exc:
        return {"capture_error": str(exc)[:300]}


def _compute_quality_gate(
    *,
    semantic_completeness_score: float | None,
    has_real_transcript: bool,
    has_visual_semantics: bool,
    degraded_stages: list[str] | None,
    artifact_status: str | None = None,
) -> tuple[bool, str]:
    """Decide canonical_artifacts.quality_gate_{passed,outcome} from
    the data we already compute during persist/enrichment.

    Previously these defaulted to (False, NULL) and were never updated,
    so EVERY completed artifact rendered as "Quality gate: failed —
    processing degraded" in the UI even when the workflow ran clean.

    Decision matrix (clear over clever):
      - completeness < 0.3 OR transcript missing entirely → needs_review
      - transcript present + visual semantics + completeness ≥ 0.5
        AND no degraded stages                             → pass
      - everything else (degraded but usable)              → pass_with_warnings

    `pass_with_warnings` still sets passed=True because the artifact IS
    usable; the UI should show a yellow/amber state, not red. Only
    `needs_review` flips passed=False and renders the red banner.
    """
    score = float(semantic_completeness_score or 0.0)
    degraded = list(degraded_stages or [])
    # Hard floor: a workflow with no transcript text or near-zero score
    # is not safe to act on — flag for human review.
    if score < 0.3 or not has_real_transcript:
        return False, "needs_review"
    # Minimal-only artifacts haven't been enriched yet; the quality
    # gate is moot until update_artifact_enriched runs. Treat as pass
    # so the UI doesn't show a red banner during the enrichment window.
    if (artifact_status or "").lower() == "minimal":
        return True, "pass_with_warnings"
    if degraded or not has_visual_semantics or score < 0.5:
        return True, "pass_with_warnings"
    return True, "pass"


async def _ensure_tenant_exists(engine: "SpineEngine", tenant_id: str) -> None:
    """Bootstrap a tenant row when it doesn't exist yet.

    Architect followup: canonical_artifacts.tenant_id has a FK to the
    `tenants` table. Before this guard, the first upload from a fresh
    tenant_id (test fixture, new customer, internal canary) would hit
    a ForeignKeyViolationError and quarantine the workflow at
    spine.persist_minimal_artifact. Now we UPSERT the tenant row at
    the start of persistence so the FK is always satisfied.

    Auto-bootstrapped rows are flagged with `name='Auto-provisioned <id>'`
    so operators can see which tenants slipped through the registration
    path. INSERT ... ON CONFLICT DO NOTHING is a no-op for existing rows.
    """
    if not tenant_id:
        return
    if not hasattr(engine, "db_pool") or not engine.db_pool:
        return
    try:
        from sqlalchemy import text
        # tenants has NOT NULL `domain` with a UNIQUE constraint, plus
        # NOT NULL `name`. Use the tenant_id itself as the synthetic
        # domain so each auto-bootstrapped row stays unique without
        # collisions. ON CONFLICT (tenant_id) catches the common case;
        # ON CONFLICT (domain) would only trigger if two distinct
        # tenant_ids hashed to the same synthetic domain — impossible
        # given we use the tenant_id verbatim.
        async with engine.db_pool() as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, name, domain, plan, status) "
                    "VALUES (:tid, :name, :domain, 'starter', 'active') "
                    "ON CONFLICT (tenant_id) DO NOTHING"
                ),
                {
                    "tid": tenant_id,
                    "name": f"Auto-provisioned {tenant_id}",
                    "domain": f"auto.{tenant_id}.nexus.internal",
                },
            )
            await session.commit()
    except Exception as exc:
        # Non-fatal — if the table doesn't exist or the connection
        # flakes, the FK INSERT will raise its own error and the
        # caller's exception handler logs the real cause. We just
        # tried to give it a soft landing.
        import logging as _logging
        _logging.getLogger("spine").debug(
            "spine.tenant_bootstrap.skipped tenant=%s err=%s", tenant_id, exc,
        )


async def _persist_artifact_to_db(
    engine: "SpineEngine", data: dict
) -> bool:
    """Persist a canonical artifact to PostgreSQL."""
    try:
        if not hasattr(engine, "db_pool") or not engine.db_pool:
            return False

        # Architect followup: ensure the tenant row exists before INSERT.
        # canonical_artifacts.tenant_id has a FK to tenants; without
        # bootstrap, every upload from a new/unregistered tenant
        # quarantined at persist_minimal_artifact.
        await _ensure_tenant_exists(engine, data.get("tenant_id", ""))

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

            # Compute quality gate from the data we just derived. Caller
            # can override by passing explicit quality_gate_passed +
            # quality_gate_outcome (e.g. brain.quality_gate step result),
            # but the default is no longer the previous `(False, NULL)`
            # — which made every artifact render as "Quality gate: failed
            # — processing degraded" in the UI regardless of outcome.
            _qg_status = data.get("status", "completed")
            _qg_degraded = list(data.get("degraded_stages") or [])
            _computed_passed, _computed_outcome = _compute_quality_gate(
                semantic_completeness_score=semantic_score,
                has_real_transcript=has_real_transcript,
                has_visual_semantics=has_visual,
                degraded_stages=_qg_degraded,
                artifact_status=_qg_status,
            )
            quality_gate_passed = bool(
                data.get("quality_gate_passed", _computed_passed)
            )
            quality_gate_outcome = (
                data.get("quality_gate_outcome") or _computed_outcome
            )

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
                quality_gate_passed=quality_gate_passed,
                quality_gate_outcome=quality_gate_outcome,
                has_real_transcript=has_real_transcript,
                has_visual_semantics=has_visual,
                semantic_completeness_score=semantic_score,
                full_artifact_json=data.get("full_artifact_json"),
                processing_time_seconds=data.get("processing_time_seconds"),
            )
            session.add(row)
            await session.commit()
            return True
    except Exception as exc:
        # NOTE: this module doesn't bind a top-level `logger`; the rest
        # of the file uses logging.getLogger("spine") inline. The
        # earlier code shadowed that with a bare `logger.error(...)`
        # call which raised NameError on the FK-violation path and
        # masked the real error. Use the same pattern as elsewhere.
        import logging as _logging
        _logging.getLogger("spine").error(
            "spine._persist_artifact_to_db.failed artifact_id=%s err=%s",
            data.get("artifact_id"), exc, exc_info=True,
        )
        raise


async def _update_artifact_enrichment_in_db(
    engine: "SpineEngine",
    tenant_id: str,
    artifact_id: str,
    update_fields: dict,
    full_json_patch: dict,
) -> bool:
    """Phase 3: update an already-persisted canonical_artifacts row
    with enrichment data (visual_analysis, visual_graph, etc).
    Idempotent — safe to retry. Returns True if a row was updated."""
    try:
        if not hasattr(engine, "db_pool") or not engine.db_pool:
            return False
        from nexus_sdk.db.models import CanonicalArtifactRow
        from sqlalchemy import select, update as sa_update
        async with engine.db_pool() as session:
            # Load existing row to merge full_artifact_json (the
            # JSONB column is opaque to SQL — easiest to read, merge,
            # write back).
            stmt = select(CanonicalArtifactRow).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if not existing:
                logger.warning(
                    "spine._update_artifact_enrichment_in_db: artifact not found",
                    extra={"artifact_id": artifact_id, "tenant_id": tenant_id},
                )
                return False
            # Merge JSONB column.
            merged_json = dict(existing.full_artifact_json or {})
            merged_json.update(full_json_patch)
            # Recompute semantic-completeness flags now that we have
            # visual data.
            visual = update_fields.get("visual_summary", "") or existing.visual_summary or ""
            scene_count = update_fields.get("scene_count", 0) or existing.scene_count or 0
            has_visual = bool(
                visual and scene_count > 0 and "[stub" not in visual.lower()
            )
            transcript = existing.safe_transcript_text or ""
            has_real_transcript = bool(
                transcript and len(transcript.split()) >= 10
                and "[stub" not in transcript.lower()
            )
            t_score = min(len(transcript.split()) / 100, 1.0) if has_real_transcript else 0.0
            v_score = min(scene_count / 5, 1.0) if has_visual else 0.0
            m_score = 1.0 if (existing.duration_seconds or 0) > 0 else 0.0
            semantic_score = round(t_score * 0.5 + v_score * 0.3 + m_score * 0.2, 3)
            # Compute quality gate at enrichment time. The minimal-persist
            # path defaulted to (False, NULL) by design — the gate isn't
            # meaningful until enrichment runs. Now that we have the full
            # picture (visual + transcript + degraded state), set it once.
            _final_status = update_fields.get("status", "persisted")
            _degraded = list(
                (full_json_patch or {}).get("degraded_stages") or []
            )
            _qg_passed, _qg_outcome = _compute_quality_gate(
                semantic_completeness_score=semantic_score,
                has_real_transcript=has_real_transcript,
                has_visual_semantics=has_visual,
                degraded_stages=_degraded,
                artifact_status=_final_status,
            )

            await session.execute(
                sa_update(CanonicalArtifactRow)
                .where(CanonicalArtifactRow.artifact_id == artifact_id)
                .where(CanonicalArtifactRow.tenant_id == tenant_id)
                .values(
                    status=_final_status,
                    scene_count=scene_count,
                    frame_count=update_fields.get("frame_count", existing.frame_count or 0),
                    visual_summary=visual,
                    application_types_seen=update_fields.get(
                        "application_types_seen", existing.application_types_seen or []
                    ),
                    has_visual_semantics=has_visual,
                    semantic_completeness_score=semantic_score,
                    quality_gate_passed=_qg_passed,
                    quality_gate_outcome=_qg_outcome,
                    full_artifact_json=merged_json,
                )
            )
            await session.commit()
            return True
    except Exception as exc:
        import logging as _logging
        _logging.getLogger("spine").error(
            "spine._update_artifact_enrichment_in_db.failed artifact_id=%s err=%s",
            artifact_id, exc, exc_info=True,
        )
        raise


async def _read_canonical_artifact(
    engine: "SpineEngine", tenant_id: str, artifact_id: str,
) -> Optional[dict]:
    """Read a small projection of the canonical_artifacts row —
    used after enrichment update to repopulate the fingerprint cache."""
    try:
        if not hasattr(engine, "db_pool") or not engine.db_pool:
            return None
        from nexus_sdk.db.models import CanonicalArtifactRow
        from sqlalchemy import select
        async with engine.db_pool() as session:
            stmt = select(
                CanonicalArtifactRow.artifact_id,
                CanonicalArtifactRow.tenant_id,
                CanonicalArtifactRow.session_id,
                CanonicalArtifactRow.media_fingerprint,
                CanonicalArtifactRow.status,
            ).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
            result = await session.execute(stmt)
            row = result.first()
            if not row:
                return None
            return {
                "artifact_id": row.artifact_id,
                "tenant_id": row.tenant_id,
                "session_id": row.session_id,
                "media_fingerprint": row.media_fingerprint,
                "status": row.status,
            }
    except Exception:
        return None


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


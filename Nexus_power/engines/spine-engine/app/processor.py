"""
Spine Engine — Document Processor.

Orchestrates the full document processing pipeline:
1. Detect format & parse using the correct parser
2. Classify document type via keyword scoring
3. Chunk text into overlapping segments for embedding / vector storage
4. Extract and structure tables
5. Build full source references for provenance

All parsers are dynamically selected from the PARSER_MAP.
"""

from __future__ import annotations

import hashlib
from typing import Optional
from enum import Enum

from nexus_sdk.models import SourceReference, Confidence

from .models import DocumentChunk, ExtractedTable
from .parsers import (
    PDFParser, ExcelParser, WordParser, PowerPointParser, CSVParser, TextParser,
)


class DocumentProcessor:
    """Main processor that coordinates parsing, chunking, classification."""

    PARSER_MAP = {
        "pdf": PDFParser,
        "xlsx": ExcelParser,
        "xls": ExcelParser,
        "docx": WordParser,
        "doc": WordParser,
        "pptx": PowerPointParser,
        "ppt": PowerPointParser,
        "csv": CSVParser,
        "txt": TextParser,
        "md": TextParser,
    }

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def process(
        self,
        content: bytes,
        filename: str,
        document_id: str,
        tenant_id: str,
        session_id: Optional[str] = None,
        classification_keywords: Optional[dict] = None,
    ) -> dict:
        """
        Full document processing pipeline:
        1. Detect format & parse
        2. Classify document type
        3. Chunk text for embeddings
        4. Extract tables
        5. Build source references
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

        parser_class = self.PARSER_MAP.get(ext)
        if not parser_class:
            raise ValueError(f"Unsupported format: {ext}")

        # Step 1: Parse
        parsed = parser_class.parse(content, document_id)
        full_text = parsed.get("full_text", "")
        tables = parsed.get("tables", [])
        page_count = parsed.get("page_count", 0)

        # Step 2: Classify
        kw = classification_keywords or {}
        doc_type = self._classify_document(full_text, filename, tables, kw)

        # Step 3: Chunk
        chunks = self._chunk_text(full_text, document_id, tenant_id, session_id, parsed)

        # Step 4: Process tables
        for table in tables:
            table.document_id = document_id

        return {
            "document_id": document_id,
            "filename": filename,
            "document_type": doc_type,
            "page_count": page_count,
            "chunks": chunks,
            "tables": tables,
            "full_text_length": len(full_text),
            "file_hash": hashlib.sha256(content).hexdigest(),
        }

    def _classify_document(
        self,
        text: str,
        filename: str,
        tables: list[ExtractedTable],
        classification_keywords: dict | None = None,
    ) -> object:
        """Classify document type using keyword scoring."""
        if classification_keywords is None:
            # Fall back to default keywords from main config
            try:
                from main import CLASSIFICATION_KEYWORDS
                classification_keywords = CLASSIFICATION_KEYWORDS
            except (ImportError, AttributeError):
                classification_keywords = {}
        text_lower = text.lower()
        filename_lower = filename.lower()

        scores: dict[str, int] = {}
        for doc_type, keywords in classification_keywords.items():
            score = 0
            kw_list = keywords if isinstance(keywords, list) else []
            for kw in kw_list:
                if kw in text_lower:
                    score += 2
                if kw in filename_lower:
                    score += 5  # Filename match is strong signal
            scores[doc_type] = score

        # Heuristic boosts
        rate_table_key = None
        training_deck_key = None
        for dt in classification_keywords:
            dt_val = dt.value if isinstance(dt, Enum) else str(dt)
            if dt_val == "rate_table":
                rate_table_key = dt
            if dt_val == "training_deck":
                training_deck_key = dt

        if rate_table_key and tables and any(
            any("age" in h.lower() or "premium" in h.lower() for h in t.headers)
            for t in tables
        ):
            scores[rate_table_key] = scores.get(rate_table_key, 0) + 10

        if rate_table_key and any(ext in filename_lower for ext in [".xlsx", ".xls", ".csv"]):
            scores[rate_table_key] = scores.get(rate_table_key, 0) + 3

        if training_deck_key and any(ext in filename_lower for ext in [".pptx", ".ppt"]):
            scores[training_deck_key] = scores.get(training_deck_key, 0) + 5

        best_type = max(scores, key=scores.get) if scores else None
        best_score = scores.get(best_type, 0) if best_type else 0

        if best_score < 3:
            # Return "general" — caller will map to the correct enum
            return "general"
        return best_type

    def _chunk_text(
        self,
        text: str,
        document_id: str,
        tenant_id: str,
        session_id: Optional[str],
        parsed: dict,
    ) -> list[DocumentChunk]:
        """Split text into overlapping chunks for embedding."""
        if not text:
            return []

        chunks: list[DocumentChunk] = []
        pages = parsed.get("pages", parsed.get("slides", []))

        if pages:
            # Page-aware chunking
            for page in pages:
                page_text = page.get("text", "")
                page_num = page.get("page_number", page.get("slide_number", None))
                section = page.get("section", page.get("title", None))

                for chunk_text in self._split_text(page_text):
                    chunks.append(DocumentChunk(
                        document_id=document_id,
                        chunk_type="text",
                        content=chunk_text,
                        page_number=page_num,
                        section=section,
                        char_count=len(chunk_text),
                        source=SourceReference(
                            document_id=document_id,
                            session_id=session_id,
                            page_number=page_num,
                            confidence=Confidence.HIGH,
                        ),
                    ))
        else:
            # Simple chunking
            for chunk_text in self._split_text(text):
                chunks.append(DocumentChunk(
                    document_id=document_id,
                    chunk_type="text",
                    content=chunk_text,
                    char_count=len(chunk_text),
                    source=SourceReference(
                        document_id=document_id,
                        session_id=session_id,
                        confidence=Confidence.MEDIUM,
                    ),
                ))

        return chunks

    def _split_text(self, text: str) -> list[str]:
        """Split text into overlapping chunks, respecting sentence boundaries."""
        if len(text) <= self.chunk_size:
            return [text] if text.strip() else []

        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size

            # Try to break at sentence boundary
            if end < len(text):
                for delim in [". ", ".\n", "? ", "!\n", "\n\n"]:
                    last_delim = text.rfind(delim, start + self.chunk_size // 2, end)
                    if last_delim != -1:
                        end = last_delim + len(delim)
                        break

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            start = end - self.chunk_overlap
            if start >= len(text):
                break

        return chunks

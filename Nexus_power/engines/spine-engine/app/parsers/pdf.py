"""
Spine Engine — PDF Parser.

Parses PDF documents into text chunks and tables using PyMuPDF (fitz).
Falls back to stub data if the library is not available.
"""

from __future__ import annotations

import logging

from nexus_sdk.events import fire_stub_alert

from ..models import ExtractedTable

logger = logging.getLogger("nexus.spine.parsers.pdf")

# Module-level event bus and stub tracking — injected by SpineEngine.on_startup()
_event_bus = None
_stub_counts: dict[str, int] = {}


def set_event_bus(bus) -> None:
    """Called by SpineEngine.on_startup() to inject the event bus reference."""
    global _event_bus
    _event_bus = bus


class PDFParser:
    """Parse PDF documents into text chunks and tables."""

    @staticmethod
    def parse(content: bytes, document_id: str) -> dict:
        """
        Parse PDF content. Uses PyMuPDF (fitz) for text extraction.
        Falls back to stub data if library not available.
        """
        try:
            import fitz  # type: ignore[import-not-found]  # PyMuPDF
            return PDFParser._parse_with_pymupdf(content, document_id)
        except ImportError:
            return PDFParser._stub_parse(content, document_id)

    @staticmethod
    def _parse_with_pymupdf(content: bytes, document_id: str) -> dict:
        """Real PDF parsing with PyMuPDF."""
        import fitz  # type: ignore[import-not-found]

        doc = fitz.open(stream=content, filetype="pdf")
        pages = []
        tables = []
        full_text = ""

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            text = page.get_text("text")
            pages.append({
                "page_number": page_num + 1,
                "text": text,
                "char_count": len(text),
            })
            full_text += text + "\n"

            # Extract tables (heuristic: look for tab-separated data)
            tab_lines = [l for l in text.split("\n") if "\t" in l or "  " in l]
            if len(tab_lines) >= 3:
                rows = []
                for line in tab_lines:
                    cells = [c.strip() for c in line.split("\t") if c.strip()]
                    if not cells:
                        cells = [c.strip() for c in line.split("  ") if c.strip()]
                    if cells:
                        rows.append(cells)
                if rows:
                    headers = rows[0] if rows else []
                    tables.append(ExtractedTable(
                        document_id=document_id,
                        page_number=page_num + 1,
                        headers=headers,
                        rows=rows[1:],
                        row_count=len(rows) - 1,
                        col_count=len(headers),
                    ))

        doc.close()

        return {
            "pages": pages,
            "tables": tables,
            "full_text": full_text,
            "page_count": len(pages),
        }

    @staticmethod
    def _stub_parse(content: bytes, document_id: str) -> dict:
        """Stub parser for development without PyMuPDF."""
        _stub_counts["pdf"] = _stub_counts.get("pdf", 0) + 1
        logger.warning("spine: PDF parser stub fallback #%d", _stub_counts["pdf"])
        fire_stub_alert(
            _event_bus, "spine", "pdf_parser",
            fallback_count=_stub_counts["pdf"],
            reason="PyMuPDF not installed",
        )
        # Estimate page count from file size (rough: ~3KB per page)
        page_count = max(1, len(content) // 3000)
        pages = []
        for i in range(min(page_count, 50)):
            pages.append({
                "page_number": i + 1,
                "text": f"[Stub PDF page {i + 1} content — install PyMuPDF for real parsing]",
                "char_count": 60,
            })

        return {
            "pages": pages,
            "tables": [],
            "full_text": " ".join(p["text"] for p in pages),
            "page_count": len(pages),
        }

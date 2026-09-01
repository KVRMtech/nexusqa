"""
Spine Engine — app/ sub-package.

Modular decomposition:
  - models.py       — Shared Pydantic models (DocumentChunk, ExtractedTable)
  - parsers/         — Format-specific document parsers (PDF, Excel, Word, etc.)
  - processor.py     — Document processing pipeline (parse → classify → chunk)
"""

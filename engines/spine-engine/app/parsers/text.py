"""
Spine Engine — Text / Markdown Parser.

Parses plain text and markdown files. No external dependencies.
"""

from __future__ import annotations


class TextParser:
    """Parse plain text / markdown files."""

    @staticmethod
    def parse(content: bytes, document_id: str) -> dict:
        text = content.decode("utf-8", errors="replace")
        return {
            "full_text": text,
            "page_count": max(1, len(text) // 3000),
            "tables": [],
        }

"""
Spine Engine — CSV Parser.

Parses CSV files into structured tables. Uses Python's built-in csv module.
"""

from __future__ import annotations

import io
import csv

from ..models import ExtractedTable


class CSVParser:
    """Parse CSV files."""

    @staticmethod
    def parse(content: bytes, document_id: str) -> dict:
        text = content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)

        tables = []
        if rows:
            headers = rows[0]
            data_rows = rows[1:]
            tables.append(ExtractedTable(
                document_id=document_id,
                headers=headers,
                rows=data_rows,
                row_count=len(data_rows),
                col_count=len(headers),
            ))

        return {
            "tables": tables,
            "full_text": text[:50_000],  # Cap text
            "page_count": 1,
        }

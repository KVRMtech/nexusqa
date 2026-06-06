"""Excel / CSV / JSON exporters in the standard QA test-case format.

Column layout (exactly as specified):

    S.No | Test Case Name | Test Case Description | Test Steps | Test Data | Expected Result

One row per step; the Test Case Name + Description cells are merged across the
steps of a test case (Excel) so the sheet reads like a hand-authored test
plan.  Uses ``openpyxl`` directly (already a dependency) — full control over
the format, no dependency on a producer-specific contract.
"""

from __future__ import annotations

import csv
import io
from typing import Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from nexus_sdk.models import ProductionTestCase

HEADERS = [
    "S.No",
    "Test Case Name",
    "Test Case Description",
    "Test Steps",
    "Test Data",
    "Expected Result",
    "Observed in Recording",
]
_COL_WIDTHS = [8, 34, 46, 62, 28, 50, 42]

# How the signal behind a step was sourced — keeps the evidence column honest.
_PROV_LABEL = {
    "demonstrated": "Observed",
    "available": "Option (captured)",
    "inferred": "Inferred",
}


def _observed_cell(step) -> str:
    """Compact evidence string captured in the recording, prefixed by provenance."""
    o = getattr(step, "observed", None) or {}
    prov = _PROV_LABEL.get(getattr(step, "provenance", "") or "", "")
    if o.get("url"):
        ev = f"navigate -> {o['url']}"
    elif o:
        tgt = f'"{o.get("label")}"' if o.get("label") else ""
        val = f' = "{o.get("value")}"' if o.get("value") else ""
        ev = f'{o.get("verb", "")} {tgt}{val}'.strip()
    else:
        ev = ""
    if prov and ev:
        return f"{prov}: {ev}"
    return prov or ev

EXPORT_MEDIA_TYPES = {
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "json": "application/json",
}

_HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_THIN = Side(style="thin", color="DDDDDD")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _expected(step) -> str:
    return (getattr(step, "expected_result", None)
            or getattr(step, "expected", None) or "")


def _step_rows(tc: ProductionTestCase) -> list[tuple]:
    """Return (s_no, name, description, action, data, expected) per step."""
    rows: list[tuple] = []
    name = tc.name or ""
    description = tc.description or ""
    steps = tc.steps or []
    if not steps:
        return [(1, name, description, "(No steps defined)", "", "", "")]
    for idx, st in enumerate(steps, start=1):
        s_no = st.step_number if st.step_number is not None else idx
        rows.append((
            s_no,
            name,
            description,
            st.action or "",
            getattr(st, "data_ref", None) or "",
            _expected(st),
            _observed_cell(st),
        ))
    return rows


def build_excel(test_cases: Sequence[ProductionTestCase]) -> bytes:
    """Render the suite to a styled .xlsx in the standard format."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Test Cases"

    for col, header in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
        cell.border = _BORDER
    for i, width in enumerate(_COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    row = 2
    for tc in test_cases:
        start = row
        for s_no, name, desc, action, data, expected, observed in _step_rows(tc):
            ws.cell(row=row, column=1, value=s_no)
            ws.cell(row=row, column=2, value=name)
            ws.cell(row=row, column=3, value=desc)
            ws.cell(row=row, column=4, value=action)
            ws.cell(row=row, column=5, value=data)
            ws.cell(row=row, column=6, value=expected)
            ws.cell(row=row, column=7, value=observed)
            row += 1
        # Merge Name + Description across this test case's steps.
        if row - start > 1:
            for col in (2, 3):
                ws.merge_cells(start_row=start, start_column=col, end_row=row - 1, end_column=col)
                ws.cell(start, col).alignment = Alignment(vertical="top", wrap_text=True)

    for r in range(2, row):
        for c in range(1, len(HEADERS) + 1):
            cell = ws.cell(r, c)
            cell.border = _BORDER
            if not (c in (2, 3)):
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    ws.freeze_panes = "A2"
    if row > 2:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{row - 1}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_csv(test_cases: Sequence[ProductionTestCase]) -> bytes:
    """Render the suite to CSV in the standard format (UTF-8 BOM for Excel)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADERS)
    for tc in test_cases:
        for row in _step_rows(tc):
            writer.writerow(list(row))
    return out.getvalue().encode("utf-8-sig")


def build_json(test_cases: Sequence[ProductionTestCase]) -> bytes:
    """Render the suite to JSON (full ProductionTestCase objects)."""
    import json
    payload = {
        "version": "1.0",
        "test_cases": [tc.model_dump(mode="json") for tc in test_cases],
    }
    return json.dumps(payload, indent=2).encode("utf-8")

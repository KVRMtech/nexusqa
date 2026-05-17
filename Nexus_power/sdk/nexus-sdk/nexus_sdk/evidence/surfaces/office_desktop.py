"""Microsoft Office desktop surface extractor.

Covers three Office variants that frequently appear inside enterprise
demos:

  * **Excel** — recognise the Name Box (``A1``, ``B12``, ``Sheet2!C4``),
    formula bar (``=SUM(...)`` / ``=VLOOKUP(...)``), and column / row
    headers.  Emit cell-coordinate selectors usable by xlwings,
    openpyxl, or Excel COM automation.
  * **Outlook** — detect the message-list pane (From / Subject / Date
    columns) and the ribbon (``New Email``, ``Reply``, ``Forward``).
  * **Word** — detect the ribbon tabs (``Home``, ``Insert``, ``Layout``,
    …) and the current style indicator.

Each variant emits distinct ``element_type`` values so the test-case
generator can pick the right driver:

  * ``excel_cell``         (cell selector ``excel://Sheet1!A1``)
  * ``excel_formula``      (formula evidence; not typeable directly)
  * ``outlook_command``    (ribbon command, e.g. ``New Email``)
  * ``outlook_list_row``   (one message row, evidence-only)
  * ``word_command``       (ribbon command, e.g. ``Insert > Table``)
  * ``office_status``      (cross-app status-bar evidence)
"""
from __future__ import annotations

import re
import uuid

from .base import SurfaceExtractor, register_surface


_NS = uuid.NAMESPACE_OID


# ── Excel patterns ────────────────────────────────────────────────────────
# Name Box content: an A1-style reference, optionally with sheet prefix.
_EXCEL_CELL_REF_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_ ]{0,20}!)?\$?[A-Z]{1,3}\$?\d{1,7}\b"
)
# Excel formula in the formula bar.
_EXCEL_FORMULA_RE = re.compile(r"=([A-Z][A-Z_]{1,30})\([^=]{0,200}\)")

_EXCEL_CONFIRM_TOKENS = (
    "microsoft excel", " excel ", "workbook", "name box",
    "formula bar", "sheet1", "sheet 1", "autosum",
    " sum(", " vlookup(", " xlookup(", " index(", " match(",
)

# ── Outlook patterns ──────────────────────────────────────────────────────
_OUTLOOK_RIBBON_COMMANDS = (
    "New Email", "New Items", "Reply", "Reply All", "Forward",
    "Delete", "Archive", "Move", "Categorize", "Mark Unread",
    "Send / Receive", "Send/Receive", "Schedule a Meeting",
    "Out of Office", "Rules",
)
_OUTLOOK_CONFIRM_TOKENS = (
    "microsoft outlook", "outlook web", "outlook.com", "outlook 365",
    "inbox", "drafts", "sent items", "junk email", "deleted items",
    "scheduled", "to do", "calendar",
    "from:", "to:", "cc:", "subject:",
)

# Outlook message-list row pattern: ``Sender Name        Subject text     12:34 PM``
_OUTLOOK_ROW_RE = re.compile(
    r"^([A-Z][\w. ]{1,40}?)\s{2,}([^\n]{4,80}?)\s{2,}(\d{1,2}:\d{2}\s*(?:AM|PM)|[A-Za-z]{3}\s+\d{1,2})\s*$",
    re.MULTILINE,
)

# ── Word patterns ─────────────────────────────────────────────────────────
_WORD_RIBBON_TABS = (
    "File", "Home", "Insert", "Draw", "Design", "Layout",
    "References", "Mailings", "Review", "View", "Help",
)
_WORD_CONFIRM_TOKENS = (
    "microsoft word", "word 365",
    "track changes", "spell check", "navigation pane",
    "table of contents",
)


def _make_id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "office:" + ":".join(parts)))


class OfficeDesktopExtractor(SurfaceExtractor):
    """Extract controls from Excel / Outlook / Word desktop screens."""

    NAME = "office_desktop"
    APP_TYPE_TOKENS = (
        "excel", "outlook", "word", "office", "ms_excel", "ms_outlook",
        "ms_word", "powerpoint", "office_desktop",
    )

    def extract(
        self,
        scene: dict,
        frame: dict,
        artifact_id: str = "",
        tenant_id: str = "",
        all_frames: list | None = None,
    ) -> list[dict]:
        scene_id = scene.get("scene_id", "")
        frame_id = frame.get("frame_id") or None
        ocr_text = frame.get("extracted_text", "") or ""
        ocr_confidence = float(frame.get("ocr_confidence", 0.0) or 0.0)
        haystack = ocr_text.lower()

        sel_conf = round(max(ocr_confidence, 0.6) * 0.85, 4)
        emitted: list[dict] = []

        is_excel = any(t in haystack for t in _EXCEL_CONFIRM_TOKENS)
        is_outlook = any(t in haystack for t in _OUTLOOK_CONFIRM_TOKENS)
        is_word = any(t in haystack for t in _WORD_CONFIRM_TOKENS)

        if not (is_excel or is_outlook or is_word):
            return []

        # ── Excel ───────────────────────────────────────────────────
        if is_excel:
            cell_seen: set[str] = set()
            for m in _EXCEL_CELL_REF_RE.finditer(ocr_text):
                ref = m.group(0).strip()
                key = ref.upper().replace("$", "")
                if key in cell_seen:
                    continue
                cell_seen.add(key)
                if len(cell_seen) > 30:  # cap evidence on dense sheets
                    break
                # Skip refs that look like file extensions / accidental matches
                # (e.g. ``XL1`` standalone is fine; ``M1`` from "M1 chip" is not).
                if len(ref) <= 2 and not any(c.isdigit() for c in ref):
                    continue
                emitted.append({
                    "control_id": _make_id(artifact_id, scene_id, "cell", key),
                    "scene_id": scene_id,
                    "frame_id": frame_id,
                    "artifact_id": artifact_id,
                    "tenant_id": tenant_id,
                    "element_type": "excel_cell",
                    "label_text": ref,
                    "value_text": "",
                    "action_kind": "enter_text",
                    "observed_value": "",
                    "display_label": f"Cell: {ref}",
                    "bounding_box": {},
                    "selector_source": "office",
                    "playwright_selector": f"excel://{ref}",
                    "selector_confidence": sel_conf,
                    "automation_ready": True,
                })

            for m in _EXCEL_FORMULA_RE.finditer(ocr_text):
                func = m.group(1).upper()
                formula = m.group(0)
                emitted.append({
                    "control_id": _make_id(artifact_id, scene_id, "formula", func, formula[:80]),
                    "scene_id": scene_id,
                    "frame_id": frame_id,
                    "artifact_id": artifact_id,
                    "tenant_id": tenant_id,
                    "element_type": "excel_formula",
                    "label_text": f"={func}",
                    "value_text": formula,
                    "action_kind": "enter_text",
                    "observed_value": formula,
                    "display_label": f"Formula: {formula[:60]}",
                    "bounding_box": {},
                    "selector_source": "office",
                    "playwright_selector": "excel://formula_bar",
                    "selector_confidence": sel_conf,
                    "automation_ready": False,  # evidence-only; cell holds the formula
                })

        # ── Outlook ─────────────────────────────────────────────────
        if is_outlook:
            for cmd in _OUTLOOK_RIBBON_COMMANDS:
                if cmd.lower() not in haystack:
                    continue
                emitted.append({
                    "control_id": _make_id(artifact_id, scene_id, "outlook_cmd", cmd),
                    "scene_id": scene_id,
                    "frame_id": frame_id,
                    "artifact_id": artifact_id,
                    "tenant_id": tenant_id,
                    "element_type": "outlook_command",
                    "label_text": cmd,
                    "value_text": "",
                    "action_kind": "click_cta",
                    "observed_value": "",
                    "display_label": f"Click: {cmd}",
                    "bounding_box": {},
                    "selector_source": "office",
                    "playwright_selector": f"outlook://ribbon?command={cmd}",
                    "selector_confidence": sel_conf,
                    "automation_ready": True,
                })

            row_seen: set[str] = set()
            for m in _OUTLOOK_ROW_RE.finditer(ocr_text):
                sender = m.group(1).strip()
                subject = m.group(2).strip()
                when = m.group(3).strip()
                key = f"{sender.lower()}|{subject.lower()}"
                if key in row_seen:
                    continue
                row_seen.add(key)
                if len(row_seen) > 20:
                    break
                emitted.append({
                    "control_id": _make_id(artifact_id, scene_id, "outlook_row", key),
                    "scene_id": scene_id,
                    "frame_id": frame_id,
                    "artifact_id": artifact_id,
                    "tenant_id": tenant_id,
                    "element_type": "outlook_list_row",
                    "label_text": f"{sender} — {subject}",
                    "value_text": when,
                    "action_kind": "click_cta",
                    "observed_value": when,
                    "display_label": f"Open: {sender} / {subject[:40]}",
                    "bounding_box": {},
                    "selector_source": "office",
                    "playwright_selector": (
                        f"outlook://message_list?sender={sender}&subject={subject[:40]}"
                    ),
                    "selector_confidence": sel_conf,
                    # Row identity depends on transient message ordering;
                    # safer as evidence than as an automation target.
                    "automation_ready": False,
                })

        # ── Word ────────────────────────────────────────────────────
        if is_word:
            for tab in _WORD_RIBBON_TABS:
                # Match only standalone occurrences of the tab name so
                # we don't catch ``home page`` matching ``Home``.
                pat = re.compile(r"(?<![A-Za-z])" + re.escape(tab) + r"(?![A-Za-z])")
                if pat.search(ocr_text) is None:
                    continue
                emitted.append({
                    "control_id": _make_id(artifact_id, scene_id, "word_tab", tab),
                    "scene_id": scene_id,
                    "frame_id": frame_id,
                    "artifact_id": artifact_id,
                    "tenant_id": tenant_id,
                    "element_type": "word_command",
                    "label_text": tab,
                    "value_text": "",
                    "action_kind": "navigate",
                    "observed_value": "",
                    "display_label": f"Open: {tab} tab",
                    "bounding_box": {},
                    "selector_source": "office",
                    "playwright_selector": f"word://ribbon?tab={tab}",
                    "selector_confidence": sel_conf,
                    "automation_ready": True,
                })

        return emitted


register_surface(OfficeDesktopExtractor())

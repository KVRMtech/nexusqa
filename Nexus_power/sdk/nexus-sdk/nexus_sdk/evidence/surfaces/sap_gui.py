"""SAP GUI / Oracle Forms surface extractor.

SAP GUI for Windows and Oracle Forms share a distinctive shape:

  * A **transaction-code box** at the top-left showing the current
    T-code (``SE38``, ``MM03``, ``VA01``, …) — usually 4-letter codes
    starting with one of ``M``, ``F``, ``V``, ``S``, ``P``, ``Z`` or
    the literal prefix ``/N``, ``/O``.
  * **Right-of-field labels**: SAP places the label *to the right* of the
    input field, opposite to web conventions.  This pattern is
    recognisable by OCR but inverts the usual label→value direction.
  * A **status bar** at the bottom showing system / client / language
    (``SE38  PRD  100  EN  INS``).
  * **Function key labels** in the toolbar/footer
    (``F8 Execute``, ``F3 Back``, ``F11 Save``).

We emit:

  * ``sap_tcode``     — the current T-code (read-only evidence)
  * ``sap_field``     — labelled input field
  * ``sap_function``  — function/menu key
  * ``sap_status``    — status bar values (system/client/language)

Selector format: ``sap://tcode=MM03&field=Material``.
"""
from __future__ import annotations

import re
import uuid

from .base import SurfaceExtractor, register_surface


_NS = uuid.NAMESPACE_OID

# Real SAP T-codes are 1–4 alphanumerics, often prefixed by /N or /O for
# session navigation.  Standard SAP modules use letter prefixes that
# uniquely identify them (MM = Materials, FI = Financial, etc.).  This
# regex deliberately accepts the union; we don't try to enumerate every
# T-code because customers also ship Z-customs (``ZMM_REPORT``).
_TCODE_RE = re.compile(
    r"(?:^|\s)(/[NnOo])?\s*([A-Za-z]{2,4}\d{0,3}|Z[A-Za-z0-9_]{2,8})\b(?=\s|$|[.,;:])"
)

# SAP function key in toolbar: ``F8 Execute`` or ``F3 Back``.  We also
# accept the trailing-paren style ``(F8)`` that appears in the GUI hint
# overlays.
_SAP_FN_RE = re.compile(
    r"(?:^|\s|\()F(\d{1,2})\)?\s+([A-Za-z][A-Za-z0-9 /\-_.]{2,30}?)(?=\s{2,}|$|\()"
)

# Confirmation tokens that this really is an SAP screen.  Without one of
# these we don't emit SAP-specific selectors even if APP_TYPE_TOKENS
# matched on a soft signal.
_SAP_CONFIRM_TOKENS = (
    "sap easy access", "sap logon", "sap gui",
    "transaction", "system→", "system ->",
    "/nex", "/nse38", "/nmm03",
    " mandt", " bukrs",
    "sap r/3", "sap netweaver",
    "ok-code", "ok code", "command field",
    "client    user", " client ", " language ",
)

# SAP status-bar pattern.  The standard layout puts ``SystemID  Client
# User  Language  InsOvr`` on one line at the bottom.
_STATUS_BAR_RE = re.compile(
    r"\b([A-Z][A-Z0-9]{2,7})\s+(\d{3})\s+([A-Z]{2,3})\s+(INS|OVR)\b"
)

# Field+label pattern.  SAP places the input first, then the label.
# OCR sees ``"________________   Material"`` (long blank followed by
# label).  We accept either underscores or two-plus spaces as the field
# placeholder.
_FIELD_THEN_LABEL_RE = re.compile(
    r"(?:_{3,}|\s{4,})([A-Z][A-Za-z][A-Za-z\-/ .]{1,40}?)(?=\s{2,}|$)"
)


def _make_id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "sap_gui:" + ":".join(parts)))


class SAPGuiExtractor(SurfaceExtractor):
    """Extract controls from SAP GUI for Windows / SAP Logon screens."""

    NAME = "sap_gui"
    APP_TYPE_TOKENS = (
        "sap", "saplogon", "sap_gui", "sapgui", "oracle_forms",
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

        # Confirm SAP shape before emitting SAP-specific selectors
        if not any(tok in ocr_text.lower() for tok in _SAP_CONFIRM_TOKENS):
            return []

        sel_conf = round(max(ocr_confidence, 0.6) * 0.85, 4)
        emitted: list[dict] = []
        seen_fields: set[str] = set()

        # ── 1. Transaction code ─────────────────────────────────────
        # SAP places the T-code in the top toolbar's command field; the
        # navigation shortcut prefix ``/N`` or ``/O`` is the *only*
        # unambiguous T-code marker in OCR.  If any candidate carries
        # that prefix we use it; otherwise fall back to the first
        # plausible bare candidate.  This avoids picking up the brand
        # banner "SAP Easy Access" as a transaction code.
        _NOISE_WORDS = {"the", "for", "set", "and", "but", "off", "out", "all", "sap"}
        prefixed: str = ""
        bare: str = ""
        for m in _TCODE_RE.finditer(ocr_text):
            prefix = (m.group(1) or "").upper()
            candidate = (m.group(2) or "").upper()
            if len(candidate) < 2 or candidate.lower() in _NOISE_WORDS:
                continue
            if prefix and not prefixed:
                prefixed = candidate
            elif not bare:
                bare = candidate
        current_tcode = prefixed or bare

        if current_tcode:
            emitted.append({
                "control_id": _make_id(artifact_id, scene_id, "tcode", current_tcode),
                "scene_id": scene_id,
                "frame_id": frame_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "element_type": "sap_tcode",
                "label_text": "Transaction Code",
                "value_text": current_tcode,
                "action_kind": "enter_text",
                "observed_value": current_tcode,
                "display_label": f"T-Code: {current_tcode}",
                "bounding_box": {},
                "selector_source": "sap",
                "playwright_selector": f"sap://command?tcode={current_tcode}",
                "selector_confidence": sel_conf,
                "automation_ready": True,
            })

        # ── 2. Fields (underscore-run + right-side label) ───────────
        for m in _FIELD_THEN_LABEL_RE.finditer(ocr_text):
            label = m.group(1).strip(" .:")
            if len(label) < 2 or label.lower() in seen_fields:
                continue
            # Cheap noise filter: SAP labels are typically capitalised
            # nouns or noun phrases.  Lines that contain a verb-like
            # token (4+ chars ending in -ing/-ed) are usually
            # instructional copy, not field labels.
            if re.search(r"\b\w{4,}(ing|ed)\b", label.lower()):
                continue
            seen_fields.add(label.lower())
            sel = f"sap://field?label={label}"
            if current_tcode:
                sel = f"sap://tcode={current_tcode}&field={label}"
            emitted.append({
                "control_id": _make_id(artifact_id, scene_id, "field", label),
                "scene_id": scene_id,
                "frame_id": frame_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "element_type": "sap_field",
                "label_text": label,
                "value_text": "",
                "action_kind": "enter_text",
                "observed_value": "",
                "display_label": f"Fill: {label}",
                "bounding_box": {},
                "selector_source": "sap",
                "playwright_selector": sel,
                "selector_confidence": sel_conf,
                "automation_ready": True,
            })

        # ── 3. Function keys ────────────────────────────────────────
        fk_seen: set[str] = set()
        for m in _SAP_FN_RE.finditer(ocr_text):
            key_num = m.group(1)
            label = m.group(2).strip(" .-/")
            if not label or label.isdigit():
                continue
            key_id = f"F{key_num}"
            if key_id in fk_seen:
                continue
            fk_seen.add(key_id)
            emitted.append({
                "control_id": _make_id(artifact_id, scene_id, "fn", key_id),
                "scene_id": scene_id,
                "frame_id": frame_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "element_type": "sap_function",
                "label_text": f"{key_id} {label}",
                "value_text": "",
                "action_kind": "click_cta",
                "observed_value": "",
                "display_label": f"Press: {key_id} ({label})",
                "bounding_box": {},
                "selector_source": "sap",
                "playwright_selector": f"sap://fn?key={key_id}",
                "selector_confidence": sel_conf,
                "automation_ready": True,
            })

        # ── 4. Status bar (system / client / language) ──────────────
        status_match = _STATUS_BAR_RE.search(ocr_text)
        if status_match:
            sysid, client, lang, ins = status_match.groups()
            emitted.append({
                "control_id": _make_id(artifact_id, scene_id, "status", f"{sysid}.{client}.{lang}"),
                "scene_id": scene_id,
                "frame_id": frame_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "element_type": "sap_status",
                "label_text": "Session",
                "value_text": f"{sysid} / Client {client} / {lang} / {ins}",
                "action_kind": "enter_text",
                "observed_value": f"{sysid} / Client {client} / {lang} / {ins}",
                "display_label": f"Session: {sysid} client {client} {lang} {ins}",
                "bounding_box": {},
                "selector_source": "sap",
                "playwright_selector": "sap://statusbar",
                "selector_confidence": sel_conf,
                "automation_ready": False,  # status bar is read-only evidence
            })

        return emitted


register_surface(SAPGuiExtractor())

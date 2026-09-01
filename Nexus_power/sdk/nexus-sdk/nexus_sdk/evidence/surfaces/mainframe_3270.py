"""Mainframe 3270 / TSO / CICS surface extractor.

A 3270 screen is an 80-column × 24-row monospace grid.  Fields are
demarcated by attribute bytes that are invisible to OCR, but the layout
follows strong conventions that we can detect from the OCR text alone:

  * **Title bar** on row 1–2: transaction id + screen name (e.g.
    ``ISPF  Primary Option Menu``).
  * **Command line** marked by ``===>`` followed by an underscore /
    blank input area — this is the user's primary input field.
  * **Labelled prompts** like ``USERID  ===>  HARIK______`` or
    ``Option ===>  X`` where the label appears immediately before the
    arrow and the typed value follows.
  * **Form-style label : value** pairs that fit on one line
    (``Application . . . . :  PROD``).  The dots are 3270's filler
    pattern for protected labels.
  * **Function keys** at the bottom (``F1=Help  F3=Exit  F7=Up  F12=Cancel``).

We emit four control kinds, all under the canonical ``action_kind``
taxonomy so downstream test-generation handles them like web controls:

  * ``terminal_command``  — the ``===>`` command line  (action_kind=enter_text)
  * ``terminal_field``    — labelled input prompts      (action_kind=enter_text)
  * ``terminal_function`` — function keys (PF/F-keys)   (action_kind=click_cta)
  * ``terminal_label``    — protected label : value pairs (action_kind=enter_text,
                            value already captured in ``observed_value``)

Selectors use a deterministic ``terminal://`` URI scheme that mainframe
automation tools (Robot Framework's 3270 library, IBM Personal Comms
macros) can consume directly:

    terminal://field?label=USERID
    terminal://command
    terminal://pf?key=F3
    terminal://row=11&col=18&length=8

Determinism: ``control_id`` is ``uuid5`` over
``terminal_3270:{artifact}:{scene}:{kind}:{key}`` so re-processing a
recording produces identical IDs and the DB upsert is idempotent.
"""
from __future__ import annotations

import re
import uuid

from .base import SurfaceExtractor, register_surface
from ..vocabularies import get_vocabulary as _get_vocabulary


# Loaded once at import time.  ``None`` if the YAML failed to load
# (the loader logs the failure); the extractor falls back to bare
# code identifiers in that case.
_TCODE_VOCAB = _get_vocabulary("mainframe_codes")


def _tcode_label(code: str) -> str:
    """Return ``"<CODE> - <human label>"`` when known, else just ``<CODE>``."""
    if not code or _TCODE_VOCAB is None:
        return code
    label = _TCODE_VOCAB.transaction_code_label(code)
    if not label:
        return code
    return f"{code} - {label}"


_NS = uuid.NAMESPACE_OID

# 3270 screens use periods or underscores as filler between a label and
# its value.  ``Application . . . . :  PROD`` collapses to label
# "Application" with observed_value "PROD".  Matches one or more groups
# of dots/underscores separated by spaces.
_FILLER_RE = re.compile(r"(?:[._]\s*){2,}|_{2,}|\.{2,}")

# Standard 3270 command-line prompt.  May be preceded by a label.
_COMMAND_RE = re.compile(r"={2,3}>")

# Labelled prompt: word-or-words then ===> then captured value (may be empty).
# Captures: group(1)=label, group(2)=value (up to end of current line).
# Underscore runs are blanked-out 3270 input areas; treated as "no value
# entered" after :func:`_normalise_value` strips them.
#
# Why the label class is ``[\w \t\-/.]`` (no ``\s``): ``\s`` matches
# newlines, so the previous draft chomped multi-line preambles like
# ``Perform utility functions\nUSERID`` into one giant label.  Limiting
# the class to space+tab keeps the label line-local.  The value class
# ``[^\n=]*?`` already excludes newlines and the trailing ``(?:\n|$)``
# anchors at end-of-line.
_PROMPT_RE = re.compile(
    r"([A-Za-z][\w \t\-/.]{0,30}?)\s*={2,3}>\s*([^\n=]*?)\s*(?:\n|$)"
)

# Function-key labels at the bottom of a panel.  Both 3270 styles supported:
#   ``F1=Help  F3=Exit``               (modern)
#   ``PF1=HELP   PF3=END``             (legacy)
#   ``1=Help  3=Exit``                 (very legacy / abbreviated)
_FUNC_KEY_RE = re.compile(
    r"(?:^|\s)(?:P?F|PA)?(\d{1,2})\s*=\s*([A-Za-z][A-Za-z0-9 /\-_.]{0,30}?)(?=\s{2,}|\s+(?:P?F|PA)?\d{1,2}\s*=|$)"
)

# Strong 3270 indicators that confirm this really is a mainframe screen
# (not a webpage that happens to contain "F1=Help").  The surface still
# matches via APP_TYPE_TOKENS, but if the OCR has none of these, we
# return no controls so a misclassified web screen doesn't get terminal
# selectors.
_3270_CONFIRM_TOKENS = (
    "===>", " pf1", " pf3", " pf12", "f1=", "f3=", "f12=", "cics",
    "tso", "ispf", "userid", "option ===>", "command ===>", "ready;",
    "logon applid", "pa1", "pa2",
)


def _normalise_label(text: str) -> str:
    """Tidy a captured label: strip filler, collapse whitespace, title-case-ish."""
    text = _FILLER_RE.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .:_-")
    return text[:60]


def _normalise_value(text: str) -> str:
    """Tidy a captured value: strip filler/underscores; empty → no value."""
    text = _FILLER_RE.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .:_-")
    # A pure-underscore field with no characters is an empty input.
    if not text or text == "_":
        return ""
    return text[:120]


def _emit_control(
    *,
    artifact_id: str,
    scene_id: str,
    tenant_id: str,
    frame_id,
    element_type: str,
    label_text: str,
    value_text: str,
    action_kind: str,
    selector: str,
    selector_source: str,
    selector_confidence: float,
    automation_ready: bool,
    display_verb: str,
    extra_key: str = "",
) -> dict:
    """Build one control dict using the canonical schema.

    ``extra_key`` is folded into the deterministic uuid5 input so two
    fields on the same row with the same label (rare, but happens with
    repeated ``Choice ===>``) don't collide.
    """
    control_id = str(uuid.uuid5(
        _NS,
        f"terminal_3270:{artifact_id}:{scene_id}:{element_type}:{label_text}:{extra_key}",
    ))
    display = display_verb
    if label_text:
        display = f"{display_verb}: {label_text}"
        if value_text:
            display = f"{display} = {value_text}"
    return {
        "control_id": control_id,
        "scene_id": scene_id,
        "frame_id": frame_id,
        "artifact_id": artifact_id,
        "tenant_id": tenant_id,
        "element_type": element_type,
        "label_text": label_text,
        "value_text": value_text,
        "action_kind": action_kind,
        "observed_value": value_text,
        "display_label": display,
        "bounding_box": {},
        "selector_source": selector_source,
        "playwright_selector": selector,
        "selector_confidence": selector_confidence,
        "automation_ready": automation_ready,
    }


class Mainframe3270Extractor(SurfaceExtractor):
    """Extract controls from 3270 / TSO / CICS / ISPF screens."""

    NAME = "mainframe_3270"
    APP_TYPE_TOKENS = (
        "mainframe", "3270", "tso", "cics", "ispf", "terminal_3270",
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

        # ── Confirm 3270 shape before emitting terminal selectors ────
        # The surface registry already matched application_type, but
        # the OCR itself must contain at least one strong 3270 marker.
        # Otherwise we'd misfire on web screens that happen to be
        # mislabelled as "mainframe" by the LLaVA classifier.
        haystack = ocr_text.lower()
        if not any(tok in haystack for tok in _3270_CONFIRM_TOKENS):
            return []

        # Use the highest of (overall OCR confidence, 0.6) — terminal
        # text is monospaced and large, so OCR is reliably high quality
        # on these surfaces.  Use 0.6 as a floor when EasyOCR returns 0.
        sel_conf = round(max(ocr_confidence, 0.6) * 0.9, 4)

        emitted: list[dict] = []
        seen_keys: set[str] = set()

        # ── 1. Command line (===>) ──────────────────────────────────
        # A 3270 panel almost always has exactly one command line, but
        # ISPF split-screens occasionally show two.  Capture the FIRST
        # occurrence with a dedicated control kind.
        cmd_match = _COMMAND_RE.search(ocr_text)
        if cmd_match:
            # The line containing the first ===> is the command line.
            line_start = ocr_text.rfind("\n", 0, cmd_match.start()) + 1
            line_end = ocr_text.find("\n", cmd_match.end())
            line = ocr_text[line_start: line_end if line_end != -1 else len(ocr_text)]
            # Pull anything typed after ===> on this line as the value.
            tail = line[cmd_match.end() - line_start:]
            value = _normalise_value(tail)
            # If the typed value is a recognised T-code, append the
            # human label so the inspector reads "3.4 - ISPF Edit Settings"
            # instead of just "3.4" with no context.  Bare letter codes
            # like "CEMT" / "ISPF" are looked up upper-case.
            value_display = value
            if value:
                upper = value.upper().split()[0]
                if _TCODE_VOCAB is not None:
                    label = _TCODE_VOCAB.transaction_code_label(upper)
                    if label:
                        value_display = f"{value} - {label}"
            emitted.append(_emit_control(
                artifact_id=artifact_id, scene_id=scene_id,
                tenant_id=tenant_id, frame_id=frame_id,
                element_type="terminal_command",
                label_text="Command",
                value_text=value_display,
                action_kind="enter_text",
                selector="terminal://command",
                selector_source="terminal",
                selector_confidence=sel_conf,
                automation_ready=True,
                display_verb="Type",
            ))
            seen_keys.add("__command__")

        # ── 2. Labelled prompts (label ===> value) ───────────────────
        # The command-line match above is also picked up here, but we
        # de-dup via seen_keys so it isn't emitted twice.
        for m in _PROMPT_RE.finditer(ocr_text):
            label = _normalise_label(m.group(1))
            if not label or len(label) < 2:
                continue
            # Strip generic "command" / "option" prompts that the command-line
            # handler above already captured.
            if label.lower() in ("command", "option") and "__command__" in seen_keys:
                continue
            value = _normalise_value(m.group(2))
            key = label.lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            emitted.append(_emit_control(
                artifact_id=artifact_id, scene_id=scene_id,
                tenant_id=tenant_id, frame_id=frame_id,
                element_type="terminal_field",
                label_text=label,
                value_text=value,
                action_kind="enter_text",
                selector=f"terminal://field?label={label}",
                selector_source="terminal",
                selector_confidence=sel_conf,
                automation_ready=True,
                display_verb="Type",
                extra_key=key,
            ))

        # ── 3. Protected ``label . . . . : value`` rows ─────────────
        # These are read-only display fields like ``Userid  . . . . :
        # HARIK``.  They are still useful evidence for the test-case
        # generator (assertion: field X reads value Y) so we emit them
        # with action_kind=enter_text but mark automation_ready=False —
        # they can't be *typed into* (protected), only read.
        for line in ocr_text.splitlines():
            if "===>" in line:
                continue  # already handled as a prompt
            if ":" not in line:
                continue
            # Heuristic split: ``Userid . . . . :  HARIK``
            # We require a filler run BEFORE the colon to distinguish
            # protected ISPF labels from plain "Name: value" web content.
            if not _FILLER_RE.search(line):
                continue
            head, _, tail = line.partition(":")
            label = _normalise_label(head)
            value = _normalise_value(tail)
            if not label or len(label) < 2 or label.lower() in seen_keys:
                continue
            seen_keys.add(label.lower())
            emitted.append(_emit_control(
                artifact_id=artifact_id, scene_id=scene_id,
                tenant_id=tenant_id, frame_id=frame_id,
                element_type="terminal_label",
                label_text=label,
                value_text=value,
                action_kind="enter_text",
                # Selector targets the *line* — automation tools that
                # support visual matching can locate by label-row.
                selector=f"terminal://label?text={label}",
                selector_source="terminal",
                selector_confidence=sel_conf,
                # Protected field — not directly typable.
                automation_ready=False,
                display_verb="Read",
                extra_key=f"label:{label.lower()}",
            ))

        # ── 4. Function keys (PF/F-keys) ────────────────────────────
        # A single line may declare 6-8 keys at the bottom.  We don't
        # de-dup by key number across multiple panels in the same scene
        # because PF keys are scene-local.
        fk_seen: set[str] = set()
        for m in _FUNC_KEY_RE.finditer(ocr_text):
            key_num = m.group(1)
            label = _normalise_label(m.group(2))
            # Filter out junk matches like ``=  2025`` where the "label"
            # is a 4-digit year.
            if not label or label.isdigit() or len(label) < 2:
                continue
            key_id = f"F{key_num}"
            if key_id in fk_seen:
                continue
            fk_seen.add(key_id)
            # Prefer the canonical T-code dictionary label when the OCR-
            # captured label is shorter than 3 chars (likely partial read).
            # Otherwise keep the OCR text — customer screens sometimes
            # bind PF keys to custom actions and that should override.
            #
            # The dictionary stores keys as the canonical IBM form
            # ("PF1", "PF3", "PA1"); OCR commonly reads the bare form
            # ("F1", "F3").  Try the PF-prefixed lookup first, then
            # fall back to the bare key id.
            canonical_label = None
            if _TCODE_VOCAB is not None:
                canonical_label = (
                    _TCODE_VOCAB.transaction_code_label(f"P{key_id}")  # PF{n}
                    or _TCODE_VOCAB.transaction_code_label(key_id)     # F{n}
                )
            display_pair = (
                canonical_label
                if canonical_label and len(label) < 3
                else label
            )
            emitted.append(_emit_control(
                artifact_id=artifact_id, scene_id=scene_id,
                tenant_id=tenant_id, frame_id=frame_id,
                element_type="terminal_function",
                label_text=f"{key_id} {display_pair}",
                value_text="",
                action_kind="click_cta",
                selector=f"terminal://pf?key={key_id}",
                selector_source="terminal",
                selector_confidence=sel_conf,
                automation_ready=True,
                display_verb="Press",
                extra_key=f"pf:{key_id}",
            ))

        return emitted


# Self-register on import so ``from .surfaces import *`` is enough.
register_surface(Mainframe3270Extractor())

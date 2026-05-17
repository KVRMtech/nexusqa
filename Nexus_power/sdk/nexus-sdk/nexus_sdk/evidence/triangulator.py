"""Triangulated action classifier — combine OCR + cursor + audio + LLaVA.

Each individual signal is unreliable on its own:

  * **OCR diff** sees typed values when EasyOCR can read the form field
    contents (often it can't), but says nothing about clicks
  * **Cursor events** localise where the user pointed but say nothing
    about what type of action happened (click vs drag vs hover)
  * **Audio intent** carries the SME's stated action verb but only in
    English-narrated KT sessions and only when the SME spoke
  * **LLaVA delta** describes what changed but loses precision on small
    UI text (form values, dropdown options)

The triangulator combines all four, producing one action record per
detected user-action with explicit ``agreement_score`` and per-signal
``evidence_signals`` so the frontend can render a confidence ribbon
instead of fabricating a label when only one weak signal fired.

Input shape (all optional except scene + frames):

    classify_actions_in_scene(
        scene=...,                 # the scene dict from build_scenes
        scene_frames=[...],        # ordered FrameAnalysis-shaped dicts
        cursor_events=[...],       # cursor_events for this scene's frame range
        audio_intents=[...],       # IntentEvents whose timestamp overlaps the scene
        controls=[...],            # evidence_controls already extracted in this scene
    ) -> list[TriangulatedAction]

The triangulator is pure-Python and deterministic.  No LLM calls inside —
the Vision/LLaVA delta fed in is already the LLM's output.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .build_scenes import _structural_fingerprint
from .step_extractor import _filter_meaningful_tokens


# ─── Output type ─────────────────────────────────────────────────────────────

@dataclass
class TriangulatedAction:
    """One classified user action inside a scene.

    Fields mirror :class:`evidence_steps` table columns so the persistence
    layer is a 1:1 row insert.  All confidence values are 0..1.
    """

    step_index: int
    action_kind: str                       # canonical verb
    target_label: str                      # field/button name
    observed_value: str                    # typed text / selected option / button label
    confidence: float                      # weighted overall confidence
    agreement_score: float                 # fraction of signals that fired (0..1)
    evidence_signals: list[str] = field(default_factory=list)
    before_frame_id: str = ""
    after_frame_id: str = ""
    start_ms: int = 0
    end_ms: int = 0
    cursor_x: Optional[int] = None
    cursor_y: Optional[int] = None
    audio_intent_text: str = ""
    audio_intent_ts_ms: Optional[int] = None
    trigger_control_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def step_id(self) -> str:
        """Stable UUIDv4 used by the persistence layer when assigning step_id."""
        return self.metadata.get("step_id") or ""


# ─── Configuration ───────────────────────────────────────────────────────────

@dataclass
class TriangulatorConfig:
    """Tunable thresholds.  Defaults are tuned against KT recordings."""

    # Action kinds we will emit.  Any candidate from a single signal that
    # falls outside this set is dropped — keeps the downstream taxonomy
    # consistent with control_extractor / step_extractor.
    canonical_kinds: tuple[str, ...] = (
        "click_cta", "enter_text", "select_option", "submit_form",
        "navigate", "review", "scroll", "open_overlay", "close_overlay",
        "ui_interaction",
    )
    # Audio intent timestamps that fall within ``audio_align_ms`` of a
    # frame transition are bound to that transition.  500 ms is the typical
    # human reaction lag between speaking and acting in a screen-share.
    audio_align_ms: int = 1500
    # Minimum cursor confidence to count as a signal.
    cursor_min_confidence: float = 0.25
    # Minimum new-token count for OCR diff to count as a signal
    ocr_min_added_tokens: int = 1
    # LLaVA delta is signal when descriptions differ by ≥ this much
    # (Jaccard distance on tokens).  0.15 means 15% of tokens differ.
    llava_min_delta_distance: float = 0.15
    # When the action_kind is "ui_interaction" (no specific kind detected
    # but a frame transition happened), only emit when at least one
    # other signal corroborated it — otherwise we are fabricating.
    require_corroboration_for_generic: bool = True


# ─── Classifier ──────────────────────────────────────────────────────────────

class TriangulatedClassifier:
    """Combine multimodal signals into structured action records."""

    def __init__(self, config: Optional[TriangulatorConfig] = None):
        self.config = config or TriangulatorConfig()

    # ── Public API ─────────────────────────────────────────────

    def classify_actions_in_scene(
        self,
        scene: dict,
        scene_frames: list[dict],
        *,
        cursor_events: Optional[list[dict]] = None,
        audio_intents: Optional[list[dict]] = None,
        controls: Optional[list[dict]] = None,
    ) -> list[TriangulatedAction]:
        """Emit one :class:`TriangulatedAction` per detected user-action.

        Single-frame scenes (a snapshot of UI state with no observable
        interaction inside the scene boundary) emit no actions — the
        action that *led to* this scene is captured on the
        ``visual_flow_edges`` row, not here.
        """
        if not scene_frames or len(scene_frames) < 2:
            return []

        ordered_frames = sorted(scene_frames, key=lambda f: int(f.get("frame_index", 0)))
        cursor_events = sorted(
            cursor_events or [],
            key=lambda c: int(c.get("frame_index") or c.get("timestamp_ms", 0)),
        )
        audio_intents = sorted(
            audio_intents or [],
            key=lambda i: int(i.get("timestamp_ms", 0)),
        )
        controls = controls or []

        # Visual evidence is *purely visual*. Audio narration is a
        # transcript artefact; mixing it into the triangulator caused the
        # same SME utterance to be stamped on multiple evidence_steps
        # ("audio inserted into visual evidence") and let audio fabricate
        # actions that the screen didn't actually show. Audio intents are
        # accepted in the signature for backwards-compatibility but are
        # ignored here; the transcript still surfaces in the session-level
        # transcript panel and in the canonical artifact's safe_transcript.
        _audio_intents_ignored = audio_intents  # noqa: F841 — intentional
        actions: list[TriangulatedAction] = []
        for pair_index in range(1, len(ordered_frames)):
            prev = ordered_frames[pair_index - 1]
            curr = ordered_frames[pair_index]
            action = self._classify_frame_pair(
                pair_index=pair_index,
                step_index=len(actions),
                prev=prev,
                curr=curr,
                cursor_events=cursor_events,
                audio_intents=[],
                controls=controls,
            )
            if action is not None:
                actions.append(action)

        return actions

    # ── Per-pair classification ────────────────────────────────

    def _classify_frame_pair(
        self,
        *,
        pair_index: int,
        step_index: int,
        prev: dict,
        curr: dict,
        cursor_events: list[dict],
        audio_intents: list[dict],
        controls: list[dict],
    ) -> Optional[TriangulatedAction]:
        """Build a :class:`TriangulatedAction` for one frame pair.

        Returns ``None`` when no signal supports any user action between
        these two frames (silent gaps are NOT fabricated as actions).
        """
        ocr_signal = _ocr_diff_signal(prev, curr, self.config.ocr_min_added_tokens)
        cursor_signal = _cursor_signal(
            prev, curr, cursor_events, self.config.cursor_min_confidence,
        )
        llava_signal = _llava_delta_signal(prev, curr, self.config.llava_min_delta_distance)
        audio_signal = _audio_signal_for_pair(
            prev, curr, audio_intents, self.config.audio_align_ms,
        )

        signals_present: list[str] = []
        if ocr_signal:
            signals_present.append("ocr_diff")
        if cursor_signal:
            signals_present.append(cursor_signal["signal_name"])
        if llava_signal:
            signals_present.append("llava_delta")
        if audio_signal:
            signals_present.append("audio_intent")

        if not signals_present:
            return None

        # ── Pick action_kind + provenance ──────────────────────
        # Audio is the highest-fidelity verb signal when present.
        # Otherwise fall back to the visual signals' implied verb.
        # Phase F.4 — for every field we choose, we record:
        #   { "source": <signal-name>, "confidence": <0..1> }
        # so consumers (audit UI, compliance reports, debug tools) can
        # see exactly WHICH signal contributed each value.  The full map
        # ships in metadata_json.provenance.
        provenance: dict[str, dict[str, Any]] = {}

        action_kind = ""
        if audio_signal:
            action_kind = audio_signal["intent_kind"]
            provenance["action_kind"] = {
                "source": "audio_intent",
                "confidence": float(audio_signal.get("confidence") or 0.0),
            }
        if not action_kind and cursor_signal and cursor_signal.get("is_click"):
            action_kind = "click_cta"
            provenance["action_kind"] = {
                "source": "cursor_click",
                "confidence": float(cursor_signal.get("confidence") or 0.0),
            }
        if not action_kind and ocr_signal:
            action_kind = ocr_signal["implied_kind"]
            provenance["action_kind"] = {
                "source": "ocr_diff",
                "confidence": _ocr_implied_confidence(ocr_signal),
            }
        if not action_kind and llava_signal:
            action_kind = llava_signal.get("implied_kind") or "ui_interaction"
            provenance["action_kind"] = {
                "source": "llava_delta",
                "confidence": float(llava_signal.get("distance") or 0.0),
            }
        if not action_kind:
            action_kind = "ui_interaction"
            provenance["action_kind"] = {"source": "fallback", "confidence": 0.0}

        if action_kind not in self.config.canonical_kinds:
            action_kind = "ui_interaction"
            # Provenance source stays — only the value is normalised.

        # ── Target label (priority: audio > cursor-control > OCR-control-match > OCR > LLaVA) ──
        # When the OCR signal is the only source for the target, the raw
        # `delta_text` is a bag of newly-appeared tokens — e.g. "female
        # gender affect birth rate male" for a gender-selection step. That
        # is OCR row-overflow, not a control label. Before using it, try to
        # match the added tokens against the scene's known controls so the
        # target_label is a real button/field name (and we get a
        # trigger_control_id link as a bonus).
        target_label = ""
        ocr_matched_control: Optional[dict] = None
        if audio_signal and audio_signal.get("target_phrase"):
            target_label = audio_signal["target_phrase"]
            provenance["target_label"] = {
                "source": "audio_intent",
                "confidence": float(audio_signal.get("confidence") or 0.0),
            }
        if not target_label and cursor_signal and cursor_signal.get("nearest_control_label"):
            target_label = cursor_signal["nearest_control_label"]
            provenance["target_label"] = {
                "source": "cursor_control",
                "confidence": float(cursor_signal.get("confidence") or 0.0),
            }
        if not target_label and ocr_signal and controls:
            ocr_matched_control = _match_ocr_diff_to_control(ocr_signal, controls)
            if ocr_matched_control:
                target_label = ocr_matched_control.get("label_text") or ""
                provenance["target_label"] = {
                    "source": "ocr_control_match",
                    "confidence": _ocr_implied_confidence(ocr_signal),
                }
        if not target_label and ocr_signal and ocr_signal.get("delta_text"):
            target_label = ocr_signal["delta_text"]
            provenance["target_label"] = {
                "source": "ocr_diff",
                "confidence": _ocr_implied_confidence(ocr_signal),
            }
        if not target_label and llava_signal and llava_signal.get("element_label"):
            target_label = llava_signal["element_label"]
            provenance["target_label"] = {
                "source": "llava_delta",
                "confidence": float(llava_signal.get("distance") or 0.0),
            }
        if target_label and "target_label" not in provenance:
            provenance["target_label"] = {"source": "unknown", "confidence": 0.0}

        # ── Observed value (typed value / selection) ──────────────
        observed_value = ""
        if ocr_signal and ocr_signal.get("delta_text"):
            observed_value = ocr_signal["delta_text"]
            provenance["observed_value"] = {
                "source": "ocr_diff",
                "confidence": _ocr_implied_confidence(ocr_signal),
            }
        elif llava_signal and llava_signal.get("observed_value"):
            observed_value = llava_signal["observed_value"]
            provenance["observed_value"] = {
                "source": "llava_delta",
                "confidence": float(llava_signal.get("distance") or 0.0),
            }

        # ── Cursor-coordinate provenance ──────────────────────────
        if cursor_signal and cursor_signal.get("cursor_x") is not None:
            provenance["cursor_xy"] = {
                "source": "cursor_event",
                "confidence": float(cursor_signal.get("confidence") or 0.0),
            }
        # ── Audio-anchor provenance ───────────────────────────────
        if audio_signal:
            provenance["audio_anchor"] = {
                "source": "audio_intent",
                "confidence": float(audio_signal.get("confidence") or 0.0),
            }

        # ── Confidence + agreement ────────────────────────────────
        # agreement = fraction of the four supported signals that fired.
        agreement = round(len(set(_canonical_signal_names(signals_present))) / 4.0, 3)
        confidence = _combine_confidence(
            ocr_signal=ocr_signal,
            cursor_signal=cursor_signal,
            llava_signal=llava_signal,
            audio_signal=audio_signal,
        )

        # Generic ui_interaction only emits when corroborated.  This is the
        # difference between "we saw a frame change" (fabrication risk) and
        # "we saw a frame change AND something else confirmed an action".
        if (
            action_kind == "ui_interaction"
            and self.config.require_corroboration_for_generic
            and len(signals_present) < 2
        ):
            return None

        # OCR-diff is unreliable on screen-capture text (compression
        # artefacts, anti-aliasing) and routinely produces fragments like
        # "Get a", "senter", "fityour", "111", "yout" — meaningless to a
        # human reviewer looking at the bottom panel. We previously kept
        # 1-3 token short deltas hoping they were real selections like
        # "Female" / "California", but in practice the noise rate is too
        # high. Restrict OCR-only emission to cases where the target
        # was resolved against a real control (ocr_control_match) — that
        # branch DOES surface single-token selections cleanly because the
        # control's label is the ground truth, and the OCR delta only
        # needs to confirm a value appeared. All other OCR-only paths
        # (target sourced from raw delta_text) are now dropped.
        target_src = provenance.get("target_label", {}).get("source", "")
        if (
            len(signals_present) == 1
            and signals_present[0] == "ocr_diff"
            and target_src != "ocr_control_match"
        ):
            return None

        # ── Cursor coordinates + control link ─────────────────────
        cursor_x = cursor_y = None
        trigger_control_id: Optional[str] = None
        if cursor_signal:
            cursor_x = cursor_signal.get("cursor_x")
            cursor_y = cursor_signal.get("cursor_y")
            trigger_control_id = cursor_signal.get("nearest_control_id")
        # OCR-match path also resolves the control link so downstream test
        # generation can reference the same EvidenceControl row.
        if trigger_control_id is None and ocr_matched_control:
            trigger_control_id = ocr_matched_control.get("control_id") or None

        # ── Audio anchor ──────────────────────────────────────────
        audio_intent_text = ""
        audio_intent_ts_ms: Optional[int] = None
        if audio_signal:
            audio_intent_text = audio_signal.get("raw_text", "")
            audio_intent_ts_ms = audio_signal.get("timestamp_ms")

        return TriangulatedAction(
            step_index=step_index,
            action_kind=action_kind,
            target_label=target_label[:500],
            observed_value=observed_value[:2000],
            confidence=round(min(1.0, max(0.0, confidence)), 3),
            agreement_score=agreement,
            evidence_signals=list(_canonical_signal_names(signals_present)),
            before_frame_id=str(prev.get("frame_id") or ""),
            after_frame_id=str(curr.get("frame_id") or ""),
            start_ms=int(_ts_ms(prev)),
            end_ms=int(_ts_ms(curr)),
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            audio_intent_text=audio_intent_text,
            audio_intent_ts_ms=audio_intent_ts_ms,
            trigger_control_id=trigger_control_id,
            metadata={
                "step_id": str(uuid.uuid4()),
                # Phase F.4 — full per-field derivation trace.  Each entry
                # records which signal supplied the value plus that signal's
                # local confidence.  Audit / compliance UIs render this as
                # a "where did this come from?" badge per field.
                "provenance": provenance,
            },
        )


# ─── Per-signal extractors ───────────────────────────────────────────────────

def _ts_ms(frame: dict) -> int:
    """Timestamp in milliseconds for a FrameAnalysis-shaped dict."""
    if "timestamp_ms" in frame and frame["timestamp_ms"] is not None:
        try:
            return int(frame["timestamp_ms"])
        except (TypeError, ValueError):
            pass
    if "timestamp_seconds" in frame and frame["timestamp_seconds"] is not None:
        try:
            return int(round(float(frame["timestamp_seconds"]) * 1000.0))
        except (TypeError, ValueError):
            pass
    return int(frame.get("frame_index", 0))


def _ocr_diff_signal(prev: dict, curr: dict, min_added: int) -> Optional[dict]:
    """OCR added/removed token signal between two frames."""
    prev_text = (prev.get("extracted_text") or "")
    curr_text = (curr.get("extracted_text") or "")
    if prev_text == curr_text:
        return None
    prev_tokens = _filter_meaningful_tokens(_structural_fingerprint(prev_text))
    curr_tokens = _filter_meaningful_tokens(_structural_fingerprint(curr_text))
    added = curr_tokens - prev_tokens
    removed = prev_tokens - curr_tokens
    if len(added) < min_added and len(removed) < min_added:
        return None

    n_added = len(added)
    if n_added == 0 and len(removed) >= 5:
        implied_kind = "close_overlay"
    elif n_added >= 8:
        implied_kind = "open_overlay"
    elif 1 <= n_added <= 4:
        avg_len = sum(len(t) for t in added) / max(1, n_added)
        implied_kind = "enter_text" if avg_len <= 4 else "select_option"
    else:
        implied_kind = "ui_interaction"

    delta_text = " ".join(sorted(added, key=len, reverse=True)[:6])
    return {
        "implied_kind": implied_kind,
        "added_tokens": list(added),
        "removed_tokens": list(removed),
        "delta_text": delta_text,
    }


def _cursor_signal(
    prev: dict,
    curr: dict,
    cursor_events: list[dict],
    min_confidence: float,
) -> Optional[dict]:
    """Cursor click / movement signal localised to ``curr`` frame."""
    target_index = int(curr.get("frame_index", 0))
    matching = [
        c for c in cursor_events
        if int(c.get("frame_index") or -1) == target_index
        and float(c.get("confidence", 0.0)) >= min_confidence
    ]
    if not matching:
        return None
    # Choose the highest-confidence reading for this frame index.
    best = max(matching, key=lambda c: float(c.get("confidence", 0.0)))
    is_click = bool(best.get("is_click"))

    # Optional: nearest control resolution, when the caller provided
    # cursor_events that were enriched with nearest_control_id and label
    # by the orchestration layer.  The triangulator does not compute
    # nearest-control on its own — that would require the controls'
    # bounding boxes in the same coordinate space, which is the
    # orchestration layer's responsibility.
    return {
        "signal_name": "cursor_click" if is_click else "cursor_hover",
        "is_click": is_click,
        "cursor_x": int(best.get("cursor_x", 0)),
        "cursor_y": int(best.get("cursor_y", 0)),
        "nearest_control_id": best.get("nearest_control_id"),
        "nearest_control_label": best.get("nearest_control_label", ""),
        "confidence": float(best.get("confidence", 0.0)),
    }


def _llava_delta_signal(
    prev: dict, curr: dict, min_distance: float,
) -> Optional[dict]:
    """LLaVA description-delta signal — fires only when per-frame LLaVA
    actually produced different descriptions for these two frames.

    With per_frame_llava off, all frames in the same scene share one
    rep-frame description; this signal correctly returns None in that
    case so the triangulator does not double-count.
    """
    prev_desc = (prev.get("description") or "").strip()
    curr_desc = (curr.get("description") or "").strip()
    if not prev_desc or not curr_desc or prev_desc == curr_desc:
        return None

    prev_tokens = _filter_meaningful_tokens(_structural_fingerprint(prev_desc))
    curr_tokens = _filter_meaningful_tokens(_structural_fingerprint(curr_desc))
    if not prev_tokens and not curr_tokens:
        return None
    union = prev_tokens | curr_tokens
    inter = prev_tokens & curr_tokens
    distance = 1.0 - (len(inter) / len(union) if union else 1.0)
    if distance < min_distance:
        return None

    added = curr_tokens - prev_tokens
    removed = prev_tokens - curr_tokens
    implied_kind = "ui_interaction"
    if added and not removed:
        implied_kind = "open_overlay"
    elif removed and not added:
        implied_kind = "close_overlay"

    return {
        "distance": round(distance, 3),
        "implied_kind": implied_kind,
        "element_label": "",
        "observed_value": " ".join(sorted(added, key=len, reverse=True)[:6]),
    }


def _audio_signal_for_pair(
    prev: dict, curr: dict, audio_intents: list[dict], align_ms: int,
) -> Optional[dict]:
    """The strongest IntentEvent whose timestamp falls within ``align_ms``
    of the frame-pair window ``[prev_ts, curr_ts]``.

    Audio is treated as the highest-fidelity action_kind signal when an
    intent timestamp aligns with a frame transition.  Multiple intents
    in the same window are collapsed to the highest-confidence one.
    """
    if not audio_intents:
        return None
    prev_ts = _ts_ms(prev)
    curr_ts = _ts_ms(curr)
    window_start = min(prev_ts, curr_ts) - align_ms
    window_end = max(prev_ts, curr_ts) + align_ms

    in_window: list[dict] = []
    for intent in audio_intents:
        ts = int(intent.get("timestamp_ms", 0))
        if window_start <= ts <= window_end:
            in_window.append(intent)
    if not in_window:
        return None
    best = max(in_window, key=lambda i: float(i.get("confidence", 0.0)))
    return {
        "intent_kind": str(best.get("intent_kind") or "").strip(),
        "target_phrase": str(best.get("target_phrase") or "").strip(),
        "raw_text": str(best.get("raw_text") or "").strip(),
        "timestamp_ms": int(best.get("timestamp_ms", 0)),
        "confidence": float(best.get("confidence", 0.0)),
    }


# ─── Control resolution ──────────────────────────────────────────────────────

def _match_ocr_diff_to_control(
    ocr_signal: dict, controls: list[dict],
) -> Optional[dict]:
    """Best-overlap match between OCR-diff tokens and a known control label.

    The OCR-diff signal exposes ``added_tokens``: the new content that
    appeared between two frames. When that bag of tokens shares words with
    a control's ``label_text`` (e.g. "Continue", "State"), the diff is
    almost certainly that control activating — pick that control rather
    than emitting the token soup as a target_label.

    Returns the matched control dict, or ``None`` when no control's label
    overlaps the added tokens. The dict has the same shape as the entries
    passed into ``classify_actions_in_scene(controls=...)``: at minimum
    ``control_id`` and ``label_text``.

    Match rules:
      * Tokenise the label the same way the OCR fingerprint is tokenised
        (chrome-stripped, content tokens, length >= 3) so noise words on
        either side don't pad the score.
      * Score = intersection / len(label_tokens). A label that is fully
        contained in the diff is the strongest signal we can get from this
        alone (score == 1.0); we then prefer the shortest such label so
        "Continue" beats "Continue to next step" when both match.
      * Threshold 0.5 — at least half the label's content words must appear
        in the diff. Below that we are likely matching incidental overlap.
    """
    added = set(ocr_signal.get("added_tokens") or [])
    if not added or not controls:
        return None
    best: Optional[tuple[float, int, dict]] = None
    for ctrl in controls:
        label = (ctrl.get("label_text") or "").strip()
        if not label:
            continue
        label_tokens = _filter_meaningful_tokens(_structural_fingerprint(label))
        if not label_tokens:
            continue
        inter = added & label_tokens
        if not inter:
            continue
        score = len(inter) / len(label_tokens)
        if score < 0.5:
            continue
        # Prefer higher score, then shorter label on tie so "Continue"
        # wins over "Continue to next step".
        key = (score, -len(label_tokens))
        if best is None or key > (best[0], -best[1]):
            best = (score, len(label_tokens), ctrl)
    return best[2] if best else None


# ─── Combination helpers ─────────────────────────────────────────────────────

def _ocr_implied_confidence(ocr_signal: dict) -> float:
    """Derived confidence number for an OCR-diff signal.

    The OCR signal itself is binary (delta exists or doesn't), so we
    derive a 0..1 score from the magnitude of the change: more added
    tokens = more confident the OCR genuinely changed.  Capped at 0.85
    because OCR alone (without cursor / audio corroboration) should
    never claim certainty.
    """
    n_added = len(ocr_signal.get("added_tokens") or [])
    n_removed = len(ocr_signal.get("removed_tokens") or [])
    magnitude = max(n_added, n_removed)
    return round(min(0.85, 0.30 + 0.05 * magnitude), 3)


def _canonical_signal_names(signals: Iterable[str]) -> set[str]:
    """Reduce the per-signal labels to the four canonical signal classes
    the triangulator agrees on.  Cursor sub-types (click vs hover) collapse
    to "cursor" so agreement_score weighs them equally with OCR/audio/LLaVA.
    """
    canon: set[str] = set()
    for name in signals:
        if name.startswith("cursor_"):
            canon.add("cursor")
        else:
            canon.add(name)
    return canon


def _combine_confidence(
    *,
    ocr_signal: Optional[dict],
    cursor_signal: Optional[dict],
    llava_signal: Optional[dict],
    audio_signal: Optional[dict],
) -> float:
    """Weighted-blend the per-signal confidences.

    Visual signals only: audio is no longer used in evidence_steps (the
    audio_signal parameter is accepted for backwards-compat and stays at
    None under the current SDK). Weights and divisors are tuned so:

      * 1 visual signal alone  → ~0.55 (acceptable but weak)
      * 2 visual signals       → ~0.78 (good)
      * all 3 visual signals   → ~0.92 (strong)

    Cursor click is the highest-fidelity visual signal (someone literally
    pointed and clicked); OCR-diff is the next; LLaVA-delta is the most
    interpretive and contributes least.
    """
    weights = {"cursor": 0.45, "ocr": 0.30, "llava": 0.25}
    components: list[float] = []
    if cursor_signal:
        cursor_conf = float(cursor_signal.get("confidence", 0.7))
        boost = 1.0 if cursor_signal.get("is_click") else 0.7
        components.append(weights["cursor"] * cursor_conf * boost)
    if ocr_signal:
        components.append(weights["ocr"])  # OCR diff is binary; weight is its confidence
    if llava_signal:
        components.append(weights["llava"] * (0.5 + float(llava_signal.get("distance", 0.2))))
    # audio_signal intentionally ignored (visual evidence is purely visual).
    _audio_ignored = audio_signal  # noqa: F841

    if not components:
        return 0.0
    base = sum(components)
    # Divisors chosen so the targets above hold for typical signal
    # confidences. 1-signal divides loosely (a confident cursor click
    # alone should still register as moderately reliable).
    if len(components) == 1:
        return min(1.0, base / 0.55)
    if len(components) == 2:
        return min(1.0, base / 0.85)
    return min(1.0, base / 1.0)

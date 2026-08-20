"""QE-Central Contained Explorer — the MANIFEST WRITER (design §3.2 §Data model).

The crawl manifest ``/work/{crawl_id}/manifest.jsonl`` is THE explorer→writer
interface: an append-only, fsync'd, ``type``-discriminated JSONL stream plus a
sidecar of PNG screenshots on the shared ``qec-crawl-storage`` volume.  The
qe-central substrate writer (``platform/qe-central/app/substrate/writer.py``) is
a dumb mapper over these records, so the record field names here MIRROR the
``ExplorationBundle`` pydantic shape (``platform/qe-central/app/substrate/
schema.py``, read 2026-07-08) 1:1 — the shared contract test
(``tests/test_crawler_logic.py``) asserts that mirror against the real schema.

Record types (``type`` discriminator):
  * ``crawl_meta``   — one at start (stop_reason="") + one terminal (stop_reason
    set); the mapper takes the LAST.  Top-level fields mirror ``ExplorationBundle``
    (crawl_id, target_url, explorer_version, config_fingerprint, frame_count).
  * ``page_state``   — mirrors ``PageState`` (+ manifest-only ``state_id`` /
    ``ax_fingerprint`` routing keys the mapper ignores).
  * ``action``       — mirrors ``ActionRecord`` (+ manifest-only ``state_id`` /
    ``to_state`` / ``screenshot_after`` / ``phase``).
  * ``screenshot``   — mirrors ``ScreenshotRef`` EXCEPT the bytes are STAGED as a
    file: ``png_base64`` is replaced by ``path`` (relative to the crawl dir).
    qe-central reads the file and base64-encodes it into the bundle (R-5:
    the untrusted browser container never uploads to the eyes namespace).
  * ``guard_event``  — audit-only (not a substrate row): the fail-closed guard's
    every abort.  * ``edge`` — audit-only navigation edge.

Safety doctrine:
  * PII is scrubbed AT SOURCE (§2.1 / R6): every value passes
    :func:`scrub_value` before it is written; password-signalled values are
    emptied (never even redacted — nothing to leak).  Detection reuses the SDK
    detector (``nexus_sdk.evidence.pii_detector``) when importable, else a
    documented regex mirror — fail-OPEN + counted, never a silent drop.
  * one MONOTONIC clock (:class:`MonotonicClock`) stamps every ms field so the
    schema's ``non_monotonic_timeline`` rule can never trip on our own writes.
  * append + ``os.fsync`` so a crash mid-crawl leaves a durable, resumable
    prefix (design §3.2 resume-from-manifest).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.jsonl"
_FRAME_STEM = "frame_{index:05d}.png"

# Record type discriminators.
REC_CRAWL_META = "crawl_meta"
REC_PAGE_STATE = "page_state"
REC_ACTION = "action"
REC_GUARD_EVENT = "guard_event"
REC_EDGE = "edge"
#: M1.3 — one permitted walk mutation (or its linked response), written to
#: the manifest BEFORE the request is released. Hash-chained; see
#: app/walk_persist.py.
REC_WALK_MUTATION = "walk_mutation"

#: A4.3 — one verified (or honestly unverified) landing on the far side of an
#: approved irreversible boundary. THE canonical journey outcome.
REC_OUTCOME_MILESTONE = "outcome_milestone"

#: M1.5 — ONE special browser event: a popup/new tab, a native dialog, a
#: download, or a page leaving the journey. One record type carrying an
#: ``event`` discriminator rather than four, because a consumer that wants "what
#: did the browser do outside the DOM" wants all four and would otherwise have
#: to know four names to ask one question.
REC_BROWSER_EVENT = "browser_event"

#: M1.7 / T-GW-03 - the RESUME CHECKPOINT: the crawl work list, snapshotted into
#: the same append-only manifest as the evidence.  Owned by
#: :mod:`app.resume_state` (which defines its shape); re-exported here so every
#: manifest record type is enumerable from one module.  ADDITIVE: every existing
#: reader dispatches on ``type`` and ignores what it does not know, so a manifest
#: carrying checkpoints maps to a byte-identical exploration bundle.
REC_CHECKPOINT = "checkpoint"

#: M3.4 / T-RS-01 - the WRITE-AHEAD CROSSING JOURNAL: one record per attempt at
#: an irreversible boundary, appended BEFORE the click and updated after it.
#:
#: WHY THIS IS NOT THE CHECKPOINT.  The checkpoint is periodic, and an
#: irreversible action cannot wait for the next period.  ``CrossingLedger``
#: reserves a boundary before the click precisely so a crash mid-crossing leaves
#: it spent - but that reservation lived only in RAM, so a killed worker took the
#: whole ledger with it and the resumed crawl re-crossed every boundary it had
#: already crossed.  Exactly-once held within one process and was lost at exactly
#: the moment it mattered.  Journaling the reservation makes the guarantee
#: survive the process that made it.
#:
#: ADDITIVE like every other type: readers dispatch on ``type`` and ignore what
#: they do not know, so a manifest carrying crossings maps to the same bundle.
REC_CROSSING = "crossing"

#: Where captured download artifacts are staged inside the crawl directory.
ARTIFACT_SUBDIR = "artifacts"
_MAX_VALUE = 1000
_PASSWORD_PLACEHOLDER = ""  # a password value is EMPTIED, never redacted-in-place


# ─── PII scrub (source-side; mirrors platform/qe-central/app/substrate/redact) ─

#: Uniform QE-Central PII class → placeholder token (matches redact.py so the
#: R6 harness and downstream consumers see one stable shape).
_PII_CLASS_BY_PATTERN: dict[str, str] = {
    "ssn_us": "ssn",
    "credit_card_4x4": "card",
    "credit_card_amex": "card",
    "email_address": "email",
    "phone_us": "phone",
    "dob_labelled": "dob",
}

#: Documented regex MIRROR used only when the SDK detector cannot be imported
#: (the explorer container is minimal by design).  Deliberately conservative —
#: over-redaction is safe, under-redaction is not.
_FALLBACK_PII: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b")),
)


def _placeholder(pii_class: str) -> str:
    return f"[REDACTED:{pii_class}]"


@dataclass(frozen=True)
class ScrubResult:
    """Outcome of scrubbing ONE value: the safe value + class counts + a
    fail-open ``errors`` count (a detector failure passes the value through,
    LOUD and counted — never silently)."""

    value: str
    counts: dict[str, int] = field(default_factory=dict)
    errors: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _scrub_with_sdk(value: str) -> Optional[ScrubResult]:
    """Try the SDK detector; return ``None`` if it is unimportable (fall back)."""
    try:
        from nexus_sdk.evidence.pii_detector import detect_pii  # type: ignore
    except Exception:
        return None
    hits = detect_pii(value)
    counts: dict[str, int] = {}
    applied: list[tuple[int, int]] = []
    out = value
    for hit in sorted(hits, key=lambda h: (h.span[1], -(h.span[1] - h.span[0])),
                      reverse=True):
        pii_class = _PII_CLASS_BY_PATTERN.get(hit.pattern_name)
        if pii_class is None:
            continue
        start, end = hit.span
        if any(s <= start and end <= e for s, e in applied):
            continue
        out = out[:start] + _placeholder(pii_class) + out[end:]
        applied.append((start, end))
        counts[pii_class] = counts.get(pii_class, 0) + 1
    return ScrubResult(value=out, counts=counts)


def _scrub_with_fallback(value: str) -> ScrubResult:
    """Regex-mirror scrub (SDK unavailable). Non-overlapping, longest-first."""
    counts: dict[str, int] = {}
    out = value
    for pii_class, pattern in _FALLBACK_PII:
        def _sub(m: "re.Match[str]") -> str:
            counts[pii_class] = counts.get(pii_class, 0) + 1
            return _placeholder(pii_class)
        out = pattern.sub(_sub, out)
    return ScrubResult(value=out, counts=counts)


def scrub_value(value: Any, *, is_secret: bool = False) -> ScrubResult:
    """Scrub ONE value at source before it is written to the manifest.

    * ``is_secret`` (a password-signalled field) → the value is EMPTIED; a
      password can never be recognised by regex and must never be persisted.
    * otherwise every detected PII span becomes ``[REDACTED:class]`` via the SDK
      detector, or the documented regex mirror when the SDK is unavailable.

    Fail-OPEN + counted: if scrubbing raises, the value passes through with an
    ``errors=1`` count so the failure is visible (a broken detector must never
    silently drop evidence), never masked.
    """
    if is_secret:
        text = "" if value is None else str(value)
        return ScrubResult(value=_PASSWORD_PLACEHOLDER, errors=0) if text else ScrubResult(value="")
    if value is None:
        return ScrubResult(value="")
    text = str(value)
    if not text:
        return ScrubResult(value="")
    try:
        result = _scrub_with_sdk(text)
        if result is None:
            result = _scrub_with_fallback(text)
        return result
    except Exception as exc:  # fail-open, loud, counted
        logger.warning("qec.emit.scrub_failed value passes UNSCRUBBED error=%s",
                       str(exc)[:300])
        return ScrubResult(value=text[:_MAX_VALUE], errors=1)


# ─── Monotonic clock (one per crawl; design §2.1 "one monotonic clock") ──────


class MonotonicClock:
    """Milliseconds elapsed since crawl start, from ``time.monotonic``.

    Monotonic + non-negative by construction, so page ``first_seen_ms`` /
    ``last_seen_ms`` and action ``timestamp_ms`` can never violate the schema's
    ``non_monotonic_timeline`` rule.  A resumed crawl seeds ``offset_ms`` from
    the last timestamp already in the manifest so resumed timestamps continue
    strictly after the prefix.
    """

    def __init__(self, offset_ms: int = 0) -> None:
        self._start = time.monotonic()
        self._offset = max(0, int(offset_ms))

    def now_ms(self) -> int:
        return self._offset + int((time.monotonic() - self._start) * 1000)


# ─── Records (MIRROR ExplorationBundle field names — schema.py) ──────────────


@dataclass
class AnchorRecord:
    """Mirrors ``schema.AnchorBundle`` {label, kind}."""

    label: str
    kind: str


@dataclass
class AfterRecord:
    """Mirrors ``schema.AfterBundle`` {outcome, detail, navigated}."""

    outcome: str
    detail: str = ""
    navigated: bool = False


@dataclass
class ScreenshotRecord:
    """Mirrors ``schema.ScreenshotRef`` {frame_index, timestamp_ms, +path}.

    DEVIATION (documented, design R-5): the bytes are staged as a FILE on the
    shared volume, so ``png_base64`` is replaced by ``path`` (relative to the
    crawl dir).  qe-central reads the file and encodes it into the bundle; the
    untrusted browser container never uploads to the eyes namespace itself.
    """

    frame_index: int
    timestamp_ms: int
    path: str


@dataclass
class ActionRecord:
    """Mirrors ``schema.ActionRecord`` + manifest-only routing fields.

    Mirrored (→ substrate ``page_actions``): subaction_index, verb, target_label,
    target_kind, value, timestamp_ms, anchor, after, url_changed, qec.
    Manifest-only (mapper ignores): state_id, to_state, screenshot_after, phase.
    """

    subaction_index: int
    verb: str
    target_kind: str
    target_label: str = ""
    value: str | None = None
    timestamp_ms: int = 0
    anchor: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    url_changed: bool = False
    qec: dict[str, Any] = field(default_factory=dict)
    # manifest-only routing
    state_id: str = ""
    to_state: str = ""
    screenshot_after: str = ""
    phase: str = "explore"


@dataclass
class PageStateRecord:
    """Mirrors ``schema.PageState`` + manifest-only routing fields.

    Manifest-only (mapper ignores): state_id, ax_fingerprint.
    """

    sequence_index: int
    location: str
    first_seen_ms: int
    last_seen_ms: int
    title: str = ""
    url_host: str = ""
    url_path: str = ""
    url_query: str = ""
    canonical_host: str = ""
    form_snapshot: dict[str, str] = field(default_factory=dict)
    form_snapshot_signals: dict[str, dict] = field(default_factory=dict)
    #: ANSWERS P1.B — rendered value nodes ``[{label, selector, text}]``.
    displayed_values: list[dict[str, str]] = field(default_factory=list)
    #: API/network mining — XHR/fetch calls observed during the visit (method,
    #: url [query-dropped + PII-scrubbed], status, resource_type, mimes, size,
    #: timestamp_ms).  DIAGNOSTICS-ONLY (mirrors ``schema.PageState``).
    network_calls: list[dict[str, str]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    # manifest-only routing
    state_id: str = ""
    ax_fingerprint: str = ""


# ─── Low-level append-only fsync writer (the pinned convention) ──────────────


def crawl_dir(work_dir: str, crawl_id: str) -> Path:
    """The per-crawl directory (created on demand)."""
    path = Path(work_dir) / crawl_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(work_dir: str, crawl_id: str) -> Path:
    return crawl_dir(work_dir, crawl_id) / MANIFEST_FILENAME


def artifact_dir(work_dir: str, crawl_id: str) -> Path:
    """The per-crawl DOWNLOAD ARTIFACT directory (created on demand).

    A sibling of ``manifest.jsonl`` on the same shared volume, so a captured
    download travels with the manifest that references it.  The manifest carries
    the path RELATIVE to the crawl directory (``artifacts/001_policy.pdf``), the
    same convention screenshots already use — an absolute path would be a
    container-local fact that means nothing to the reader downstream.
    """
    path = crawl_dir(work_dir, crawl_id) / ARTIFACT_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_record(work_dir: str, crawl_id: str, record: dict[str, Any]) -> None:
    """Append ONE record to the crawl manifest, durably (fsync).

    The pinned Stage-2 convention.  Serialises ``record`` as one canonical JSON
    line (sorted keys, ascii-safe) and fsyncs the file descriptor so a crash
    leaves a complete, resumable prefix — a half-written trailing line is the
    only possible loss and is discarded on resume (:func:`read_records`).

    Raises:
        ValueError:  ``record`` is missing its ``type`` discriminator.
        OSError:     the manifest could not be written (propagated — an
                     un-writable manifest is a hard, honest failure).
    """
    if not isinstance(record, dict) or not str(record.get("type") or "").strip():
        raise ValueError("manifest record must be a dict carrying a non-empty 'type'")
    path = manifest_path(work_dir, crawl_id)
    line = json.dumps(record, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), default=_json_default)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "value"):  # Enum-like
        return obj.value
    return str(obj)


def read_records(work_dir: str, crawl_id: str) -> list[dict[str, Any]]:
    """Read all COMPLETE manifest records (design §3.2 resume-from-manifest).

    A trailing partial line (crash mid-write) is silently discarded so a resumed
    crawl builds only on durable evidence.  Returns ``[]`` when no manifest yet.
    """
    path = manifest_path(work_dir, crawl_id)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("qec.emit.partial_manifest_line_discarded crawl_id=%s",
                               crawl_id)
                break  # a corrupt/partial line ends the durable prefix
    return records


# ─── High-level per-crawl emitter ────────────────────────────────────────────


class ManifestEmitter:
    """Stateful writer for ONE crawl: PNG staging + monotonic frame indexing +
    record emission over :func:`append_record`.

    Frame indices are job-wide unique and monotonically assigned (the schema's
    ``duplicate_frame_index`` rule), seeded past any frames already present in a
    resumed manifest.
    """

    def __init__(self, work_dir: str, crawl_id: str, clock: MonotonicClock,
                 *, next_frame_index: int = 0) -> None:
        self.work_dir = work_dir
        self.crawl_id = crawl_id
        self.clock = clock
        self._dir = crawl_dir(work_dir, crawl_id)
        self._frame_index = max(0, int(next_frame_index))
        self.frame_count = 0

    # -- screenshots -----------------------------------------------------------

    def store_screenshot(self, png_bytes: bytes, timestamp_ms: int) -> ScreenshotRecord:
        """Stage a PNG on the shared volume; return its :class:`ScreenshotRecord`.

        Raises ``ValueError`` on empty/invalid bytes so a broken capture is an
        honest failure, never a zero-byte frame the writer would later reject.
        """
        if not png_bytes:
            raise ValueError("refusing to stage an empty screenshot")
        index = self._frame_index
        self._frame_index += 1
        rel = _FRAME_STEM.format(index=index)
        (self._dir / rel).write_bytes(png_bytes)
        self.frame_count += 1
        return ScreenshotRecord(frame_index=index, timestamp_ms=int(timestamp_ms), path=rel)

    # -- typed record emission -------------------------------------------------

    def emit_crawl_meta(self, meta: dict[str, Any]) -> None:
        record = {"type": REC_CRAWL_META, **meta}
        append_record(self.work_dir, self.crawl_id, record)

    def emit_page_state(self, page: PageStateRecord) -> None:
        record = {"type": REC_PAGE_STATE, **asdict(page)}
        append_record(self.work_dir, self.crawl_id, record)

    def emit_action(self, action: ActionRecord) -> None:
        record = {"type": REC_ACTION, **asdict(action)}
        append_record(self.work_dir, self.crawl_id, record)

    def emit_guard_event(self, *, kind: str, method: str, url: str, rule_id: str,
                         severity: str = "", reason: str = "", phase: str = "") -> None:
        record = {
            "type": REC_GUARD_EVENT, "kind": kind, "method": (method or "").upper(),
            "url": (url or "")[:2000], "rule_id": rule_id, "severity": severity,
            "reason": (reason or "")[:500], "phase": phase,
            "timestamp_ms": self.clock.now_ms(),
        }
        append_record(self.work_dir, self.crawl_id, record)

    def emit_walk_mutation(self, record: dict) -> None:
        """Append one walk-mutation audit entry VERBATIM.

        No field is dropped, reordered or reformatted here: the record's
        ``entry_hash`` was computed over exactly these keys, so a courier that
        edited them would break the chain it is carrying."""
        payload = dict(record)
        payload["type"] = REC_WALK_MUTATION
        append_record(self.work_dir, self.crawl_id, payload)

    def emit_outcome_milestone(self, record: dict) -> None:
        """Append one outcome milestone VERBATIM (A4.3 / T-AC-04).

        VERBATIM for the same reason ``emit_walk_mutation`` is: this record IS
        the evidence that an irreversible action was taken and what came of it.
        A courier that reformatted it would be editing an audit entry.

        Emitted at the moment the landing is observed rather than rolled up at
        the end of the crawl, so a crawl that is cancelled, times out or crashes
        after the crossing still leaves the crossing in the manifest.  A submit
        that happened and was never written down is the one outcome this whole
        subsystem exists to make impossible.
        """
        payload = dict(record)
        payload["type"] = REC_OUTCOME_MILESTONE
        append_record(self.work_dir, self.crawl_id, payload)

    def emit_checkpoint(self, record: dict) -> None:
        """Append one resume CHECKPOINT verbatim (M1.7 / T-GW-03).

        Stamped with the monotonic clock like every other record so the manifest
        timeline stays ordered, and written through the same fsynced
        :func:`append_record` - a checkpoint that survived a crash less reliably
        than the evidence would let a resume rebuild a work list from a moment
        the evidence never reached.
        """
        payload = dict(record or {})
        payload["type"] = REC_CHECKPOINT
        payload["timestamp_ms"] = self.clock.now_ms()
        append_record(self.work_dir, self.crawl_id, payload)

    def emit_crossing(self, record: dict) -> None:
        """Append ONE crossing-journal record VERBATIM (M3.4 / T-RS-01).

        WRITE-AHEAD.  The caller emits the RESERVED record BEFORE the
        irreversible click and the CROSSED record after the landing is observed,
        so the durable manifest can never show a click the journal does not
        already know about.  The ordering is the entire guarantee: a journal
        written after the fact has a window - a kill, a timeout, an OOM - in
        which the application was mutated and nothing durable records it, and a
        resume walking that window re-submits the same application.

        Deliberately NOT best-effort.  Every other emitter here may swallow a
        write failure because the cost is lost evidence; the cost here is a
        DUPLICATE IRREVERSIBLE ACTION, so a reservation that cannot be made
        durable must stop the crossing rather than proceed unjournaled.  The
        exception propagates to :meth:`SubmitMixin.cross_boundary`, which
        refuses the crossing and records why.

        VERBATIM for the same reason ``emit_outcome_milestone`` is: the record
        IS the evidence that an irreversible action was authorised and fired.
        """
        payload = dict(record or {})
        payload["type"] = REC_CROSSING
        payload.setdefault("timestamp_ms", self.clock.now_ms())
        append_record(self.work_dir, self.crawl_id, payload)

    def emit_browser_event(self, record: dict) -> None:
        """Append ONE special-browser-event record VERBATIM (M1.5).

        VERBATIM for the same reason ``emit_walk_mutation`` is: the record IS
        the evidence that the browser did something the DOM cannot show — a tab
        was adopted, a native dialog was answered a particular way, a file left
        the application — and a courier that reformatted it would be editing an
        audit entry.  The producer (:mod:`app.page_lifecycle`) has already
        bounded and scrubbed every field.
        """
        payload = dict(record)
        payload["type"] = REC_BROWSER_EVENT
        payload.setdefault("timestamp_ms", self.clock.now_ms())
        append_record(self.work_dir, self.crawl_id, payload)

    def emit_edge(self, *, from_state: str, to_state: str, verb: str,
                  target_label: str = "") -> None:
        record = {
            "type": REC_EDGE, "from_state": from_state, "to_state": to_state,
            "verb": verb, "target_label": (target_label or "")[:500],
            "timestamp_ms": self.clock.now_ms(),
        }
        append_record(self.work_dir, self.crawl_id, record)


# ─── Action-record construction (shared by auth / forms / crawler) ───────────


def build_action_record(
    control: dict[str, Any],
    *,
    verb: str,
    value: Any,
    observation: Any,
    subaction_index: int = 0,
    phase: str = "explore",
    state_id: str = "",
    to_state: str = "",
    screenshot_after: str = "",
    timestamp_ms: int = 0,
    is_secret: bool = False,
    after_outcome: Any = None,
) -> ActionRecord:
    """Build one grounded :class:`ActionRecord` from a control + observation.

    Centralises the mapping so auth / forms / crawler stay consistent:
      * ``after`` / ``url_changed`` come from :func:`app.browser.classify_after`
        over the measured :class:`app.browser.RawObservation` (never invented) —
        or from an INJECTED ``after_outcome`` (an :class:`app.browser.AfterOutcome`)
        when the caller has already classified the effect with a phase-specific
        rule (e.g. the Phase-B submit path's
        :func:`app.browser.classify_submit_after`); the injected value is used
        verbatim, never re-classified, so the outcome stays grounded;
      * ``target_kind`` comes from :func:`app.inventory.target_kind_for`;
      * ``value`` is PII-scrubbed at source (:func:`scrub_value`); a button/link
        click passes ``value=None`` and records no value; a password fill passes
        ``is_secret=True`` and the value is emptied;
      * ``anchor`` / ``qec`` ride verbatim from the :class:`ControlRecord`.
    """
    from .browser import classify_after  # local import: keep emit import-light
    from .inventory import target_kind_for

    outcome = after_outcome if after_outcome is not None else classify_after(observation)
    if value is None:
        recorded_value: str | None = None
    else:
        recorded_value = scrub_value(value, is_secret=is_secret).value

    anchor = control.get("anchor")
    return ActionRecord(
        subaction_index=int(subaction_index),
        verb=verb,
        target_kind=target_kind_for(control),
        target_label=str(control.get("name") or "")[:500],
        value=recorded_value,
        timestamp_ms=int(timestamp_ms),
        anchor={"label": anchor["label"], "kind": anchor["kind"]} if anchor else None,
        after={"outcome": outcome.outcome, "detail": outcome.detail,
               "navigated": outcome.navigated},
        url_changed=outcome.url_changed,
        qec=dict(control.get("qec") or {}),
        state_id=state_id,
        to_state=to_state,
        screenshot_after=screenshot_after,
        phase=phase,
    )


# ─── Resume helpers ──────────────────────────────────────────────────────────


def scan_resume_state(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Derive resume seeds from an existing manifest prefix.

    Returns ``{visited_fingerprints, next_sequence_index, next_frame_index,
    last_timestamp_ms}`` so a resumed crawl continues without revisiting states
    or colliding frame indices / sequence indices (design §3.2).
    """
    visited: set[str] = set()
    next_seq = 0
    next_frame = 0
    last_ts = 0
    for rec in records or []:
        rtype = rec.get("type")
        if rtype == REC_PAGE_STATE:
            fp = str(rec.get("ax_fingerprint") or "")
            if fp:
                visited.add(fp)
            next_seq = max(next_seq, int(rec.get("sequence_index") or 0) + 1)
            last_ts = max(last_ts, int(rec.get("last_seen_ms") or 0))
            for shot in rec.get("screenshots") or []:
                next_frame = max(next_frame, int(shot.get("frame_index") or 0) + 1)
        elif rtype == REC_ACTION:
            last_ts = max(last_ts, int(rec.get("timestamp_ms") or 0))
        # REC_CHECKPOINT is deliberately NOT read here.  This function answers
        # "what had the crawl SEEN"; the work list it still had TO DO is a
        # separate question with a separate reader (:func:`app.resume_state.
        # rebuild`).  Folding a checkpoint's counters in here would double-count
        # states that are already present as page_state records.
    return {
        "visited_fingerprints": visited,
        "next_sequence_index": next_seq,
        "next_frame_index": next_frame,
        "last_timestamp_ms": last_ts,
    }

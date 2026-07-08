"""QE-Central — ExplorationBundle schema (the S1↔S2 contract, design §2.1/§3.2).

Pydantic models for the crawl evidence bundle plus the evidence-rule
validators that make dishonest bundles IMPOSSIBLE to write:

  * monotonic ``sequence_index`` across visits / ``subaction_index`` within
    a visit (the substrate UNIQUE constraints
    ``uq_page_visits_artifact_sequence_version`` and
    ``uq_page_actions_visit_version_subaction`` require distinct values —
    alembic 034_page_visits.py:156-159 / 035_page_actions.py:104-107);
  * ``verb`` / ``target_kind`` pinned to the migration-035 column
    vocabulary (columns are ``String(20)`` with documented value sets —
    035_page_actions.py:61-65);
  * ``after`` outcome-bundle presence on every action (an explorer that
    cannot say what happened after an action produced broken evidence);
    the ``anchor`` bundle stays OPTIONAL — it is only captured on
    (role, name) duplication, and its absence is judged downstream by the
    HONEST-10 auditor (REFUSE-matrix rule R2), never masked here;
  * screenshot timestamps inside their owning visit's
    ``[first_seen_ms, last_seen_ms]`` window (the factory's ``_frame_refs``
    window-join depends on it — test_factory/service.py:61-89).

Refusals are FIRST-CLASS: every broken rule raises :class:`RefusalError`
whose ``reason`` comes from the :data:`REFUSAL_REASONS` enumeration and
whose ``str()`` is the honest, human-readable reason persisted on the
``qe_explorations`` row.
"""
from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# ─── Vocabularies (pinned to the REAL substrate DDL) ───────────────────────

#: page_actions.verb value set (alembic 035_page_actions.py:61-62).
ACTION_VERBS = frozenset(
    {"click", "type", "select", "scroll", "navigate", "submit", "hover", "none"}
)

#: page_actions.target_kind value set (alembic 035_page_actions.py:64-65).
TARGET_KINDS = frozenset(
    {"button", "link", "dropdown", "text_field", "checkbox", "radio",
     "menu_item", "tab", "other"}
)

#: Verbs that MUST carry a committed value (a fill without a value is
#: broken capture; an explicitly cleared field is the empty string).
FILL_VERBS = frozenset({"type", "select"})

#: Verbs allowed to omit a target_label (page-level interactions).
LABEL_OPTIONAL_VERBS = frozenset({"navigate", "scroll", "none"})

#: crawl_id feeds ``extractor_version`` (String(50) cap), the visual-frames
#: ``job_id`` and the storage key segments — pin it to a safe charset.
CRAWL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,36}$")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ─── Refusals (first-class, enumerated) ────────────────────────────────────

#: The honest refusal-reason enumeration (design §3.1 schema module).
REFUSAL_REASONS = frozenset({
    "non_monotonic_sequence_index",
    "non_monotonic_subaction_index",
    "non_monotonic_timeline",
    "invalid_verb",
    "invalid_target_kind",
    "missing_after_bundle",
    "missing_fill_value",
    "missing_target_label",
    "invalid_location",
    "invalid_visit_window",
    "screenshot_outside_visit_window",
    "screenshot_missing_data",
    "duplicate_frame_index",
    "frame_count_mismatch",
    "invalid_crawl_id",
    "invalid_target_url",
    "invalid_extractor_version",
    "duplicate_extractor_version",
    "schema_invalid",
})


class RefusalError(ValueError):
    """An evidence rule was broken — the substrate write is REFUSED.

    ``str(exc)`` is the honest refusal reason (persisted verbatim on the
    ``qe_explorations`` row and returned in the 422 body).  Subclasses
    ``ValueError`` deliberately: raised inside a pydantic validator it is
    converted into a ``ValidationError`` (422 at the FastAPI boundary)
    instead of escaping as a 500 — refusal is never an internal error.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason if reason in REFUSAL_REASONS else "schema_invalid"
        self.detail = detail
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)


def _refuse(reason: str, detail: str) -> None:
    raise RefusalError(reason, detail)


def _require_http_url(value: str, reason: str) -> str:
    parsed = urlparse(value or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        _refuse(reason, f"expected an http(s) URL, got {value!r:.120}")
    return value


# ─── Models ────────────────────────────────────────────────────────────────


class AnchorBundle(BaseModel):
    """"Where it sits" — nearest stable landmark disambiguating the target.

    Captured ONLY when the (role, name) pair is duplicated on the page
    (§3.2 inventory contract), so its absence is legitimate evidence state.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=500)
    kind: str = Field(min_length=1, max_length=40)


class AfterBundle(BaseModel):
    """"What happened after" — the action's grounded, observed outcome.

    ``outcome`` values the factory understands: ``navigation`` (drives the
    PROVEN navigation gate — generator ``_action_navigated``),
    ``value_committed``, ``dialog_opened``, ``dom_changed``, ``none``.
    Free String(40)-shaped by design; unknown outcomes degrade honestly
    downstream (no navigation credit), they are never invented here.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: str = Field(min_length=1, max_length=40)
    detail: str = Field(default="", max_length=500)
    navigated: bool = False

    @model_validator(mode="after")
    def _normalise(self) -> "AfterBundle":
        object.__setattr__(self, "outcome", self.outcome.strip().lower())
        if self.outcome == "navigation":
            # A recorded navigation outcome IS the grounded navigated signal.
            object.__setattr__(self, "navigated", True)
        return self


class ActionRecord(BaseModel):
    """One recorded user action on a page (→ one ``page_actions`` row)."""

    model_config = ConfigDict(extra="forbid")

    subaction_index: int = Field(ge=0)
    verb: str = Field(min_length=1, max_length=20)
    target_label: str = Field(default="", max_length=500)
    target_kind: str = Field(min_length=1, max_length=20)
    value: str | None = None
    timestamp_ms: int = Field(default=0, ge=0)
    anchor: AnchorBundle | None = None
    after: AfterBundle | None = None
    url_changed: bool = False
    # Diagnostics-only capture channel (role/frame_selector/options/testid/
    # css_hint) — persisted under evidence_signals.qec, never bound on.
    qec: dict[str, Any] = Field(default_factory=dict)

    @field_validator("verb")
    @classmethod
    def _verb_vocab(cls, v: str) -> str:
        verb = v.strip().lower()
        if verb not in ACTION_VERBS:
            _refuse("invalid_verb",
                    f"verb {verb!r} not in {sorted(ACTION_VERBS)}")
        return verb

    @field_validator("target_kind")
    @classmethod
    def _kind_vocab(cls, v: str) -> str:
        kind = v.strip().lower()
        if kind not in TARGET_KINDS:
            _refuse("invalid_target_kind",
                    f"target_kind {kind!r} not in {sorted(TARGET_KINDS)}")
        return kind

    @model_validator(mode="after")
    def _evidence_rules(self) -> "ActionRecord":
        if self.after is None:
            _refuse("missing_after_bundle",
                    f"action #{self.subaction_index} ({self.verb} "
                    f"{self.target_label!r}) has no after/outcome bundle")
        if self.verb in FILL_VERBS and self.value is None:
            _refuse("missing_fill_value",
                    f"action #{self.subaction_index} ({self.verb} "
                    f"{self.target_label!r}) carries no committed value")
        if self.verb not in LABEL_OPTIONAL_VERBS and not self.target_label.strip():
            _refuse("missing_target_label",
                    f"action #{self.subaction_index} verb {self.verb!r} "
                    f"requires a target_label")
        return self


class ScreenshotRef(BaseModel):
    """One captured page screenshot (→ one ``visual_frames`` row + asset)."""

    model_config = ConfigDict(extra="forbid")

    frame_index: int = Field(ge=0)
    timestamp_ms: int = Field(ge=0)
    png_base64: str = Field(min_length=1)

    @field_validator("png_base64")
    @classmethod
    def _valid_png(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception:
            raw = b""
        if not raw.startswith(_PNG_MAGIC):
            _refuse("screenshot_missing_data",
                    "png_base64 is not a decodable PNG image")
        return v

    def decode(self) -> bytes:
        """Return the raw PNG bytes (validated at parse time)."""
        return base64.b64decode(self.png_base64, validate=True)


class PageState(BaseModel):
    """One contiguous visit to a single URL (→ one ``page_visits`` row)."""

    model_config = ConfigDict(extra="forbid")

    sequence_index: int = Field(ge=0)
    location: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=500)
    url_host: str = Field(default="", max_length=500)
    url_path: str = Field(default="", max_length=2000)
    url_query: str = Field(default="", max_length=2000)
    canonical_host: str = Field(default="", max_length=500)
    first_seen_ms: int = Field(ge=0)
    last_seen_ms: int = Field(ge=0)
    form_snapshot: dict[str, str] = Field(default_factory=dict)
    form_snapshot_signals: dict[str, dict] = Field(default_factory=dict)
    actions: list[ActionRecord] = Field(default_factory=list)
    screenshots: list[ScreenshotRef] = Field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        """Wall-clock extent of the visit (one monotonic clock, §2.1)."""
        return max(0, self.last_seen_ms - self.first_seen_ms)

    @model_validator(mode="after")
    def _evidence_rules(self) -> "PageState":
        _require_http_url(self.location, "invalid_location")
        parsed = urlparse(self.location)
        if not self.url_host:
            object.__setattr__(self, "url_host", (parsed.hostname or "").lower())
        if not self.url_path:
            object.__setattr__(self, "url_path", parsed.path or "")
        if not self.url_query:
            object.__setattr__(self, "url_query", parsed.query or "")
        if not self.canonical_host:
            object.__setattr__(self, "canonical_host", self.url_host)

        if self.last_seen_ms < self.first_seen_ms:
            _refuse("invalid_visit_window",
                    f"visit #{self.sequence_index}: last_seen_ms "
                    f"{self.last_seen_ms} < first_seen_ms {self.first_seen_ms}")

        prev = -1
        for action in self.actions:
            if action.subaction_index <= prev:
                _refuse("non_monotonic_subaction_index",
                        f"visit #{self.sequence_index}: subaction_index "
                        f"{action.subaction_index} after {prev}")
            prev = action.subaction_index

        for shot in self.screenshots:
            if not (self.first_seen_ms <= shot.timestamp_ms <= self.last_seen_ms):
                _refuse("screenshot_outside_visit_window",
                        f"visit #{self.sequence_index}: frame "
                        f"{shot.frame_index} at {shot.timestamp_ms}ms outside "
                        f"[{self.first_seen_ms}, {self.last_seen_ms}]")
        return self


class ExplorationBundle(BaseModel):
    """The complete evidence bundle for ONE crawl (design §3.1/§3.2).

    Shape is 1:1 with what the substrate writer persists — this model is
    the JSON-schema contract the explorer (S2) builds to before it exists
    (design §7.10 cross-subsystem fixtures).
    """

    model_config = ConfigDict(extra="forbid")

    crawl_id: str = Field(min_length=1, max_length=36)
    target_url: str = Field(min_length=1, max_length=2000)
    explorer_version: str = Field(min_length=1, max_length=100)
    config_fingerprint: str = Field(min_length=1, max_length=128)
    frame_count: int = Field(ge=0)
    pages: list[PageState] = Field(default_factory=list)

    @field_validator("crawl_id")
    @classmethod
    def _crawl_id_charset(cls, v: str) -> str:
        if not CRAWL_ID_PATTERN.match(v):
            _refuse("invalid_crawl_id",
                    f"crawl_id {v!r:.60} must match "
                    f"{CRAWL_ID_PATTERN.pattern}")
        return v

    @field_validator("target_url")
    @classmethod
    def _target_url_scheme(cls, v: str) -> str:
        _require_http_url(v, "invalid_target_url")
        return v

    @model_validator(mode="after")
    def _evidence_rules(self) -> "ExplorationBundle":
        prev_seq = -1
        prev_first_ms = -1
        seen_frames: set[int] = set()
        total_shots = 0
        for page in self.pages:
            if page.sequence_index <= prev_seq:
                _refuse("non_monotonic_sequence_index",
                        f"sequence_index {page.sequence_index} after "
                        f"{prev_seq} — visits must be strictly increasing")
            prev_seq = page.sequence_index
            if page.first_seen_ms < prev_first_ms:
                _refuse("non_monotonic_timeline",
                        f"visit #{page.sequence_index} starts at "
                        f"{page.first_seen_ms}ms, before the previous visit "
                        f"({prev_first_ms}ms) — one monotonic clock required")
            prev_first_ms = page.first_seen_ms
            for shot in page.screenshots:
                if shot.frame_index in seen_frames:
                    _refuse("duplicate_frame_index",
                            f"frame_index {shot.frame_index} appears twice — "
                            f"frame indices are job-wide unique")
                seen_frames.add(shot.frame_index)
                total_shots += 1
        if total_shots != self.frame_count:
            _refuse("frame_count_mismatch",
                    f"declared frame_count={self.frame_count} but the bundle "
                    f"carries {total_shots} screenshot(s)")
        return self

    def total_actions(self) -> int:
        """Number of actions across every visit."""
        return sum(len(p.actions) for p in self.pages)


# ─── Re-validation seam (defence in depth for the writer) ─────────────────


def _refusal_from_validation_error(exc: ValidationError) -> RefusalError:
    """Recover the enumerated refusal reason from a pydantic wrapper.

    Pydantic converts a :class:`RefusalError` raised inside a validator
    into a ``ValidationError`` whose message keeps our ``reason: detail``
    text — parse it back so callers always see one exception type.
    """
    for err in exc.errors():
        msg = str(err.get("msg") or "")
        candidate = msg.removeprefix("Value error, ").split(":", 1)
        if candidate and candidate[0].strip() in REFUSAL_REASONS:
            detail = candidate[1].strip() if len(candidate) > 1 else ""
            return RefusalError(candidate[0].strip(), detail)
    return RefusalError("schema_invalid", str(exc)[:500])


def validate_bundle(bundle: ExplorationBundle) -> ExplorationBundle:
    """Re-run EVERY evidence rule on ``bundle`` and return the clean copy.

    The writer calls this before touching the database so a bundle built
    with ``model_construct`` / mutated after parse (e.g. by the REFUSE
    harness) can never slip an invalid row into the substrate.  Raises
    :class:`RefusalError` with the enumerated honest reason.
    """
    try:
        return ExplorationBundle.model_validate(bundle.model_dump())
    except ValidationError as exc:
        raise _refusal_from_validation_error(exc) from exc

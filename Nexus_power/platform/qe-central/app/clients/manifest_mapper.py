"""QE-Central — explorer manifest → :class:`ExplorationBundle` mapper (PURE).

THE cross-subsystem seam (design §3.2): the contained explorer emits an
append-only, ``type``-discriminated ``manifest.jsonl`` whose record field names
MIRROR the ``ExplorationBundle`` pydantic shape 1:1
(``engines/qe-explorer/app/emit.py``, read 2026-07-08).  This module turns
those records back into a validated bundle so the Phase-0 substrate writer
(``app/substrate/writer.py``) — a dumb mapper over the bundle — can persist the
§2 substrate rows the UNCHANGED VKPower factory reads.

The function is deliberately PURE:
  * it takes the parsed records + an injected ``screenshot_loader`` (path →
    PNG bytes), so it runs with NO browser, NO filesystem coupling and NO DB;
  * every evidence rule lives in :mod:`app.substrate.schema` — this mapper only
    RESHAPES, it never invents a value or relaxes a rule.  A dishonest manifest
    (non-monotonic sequence, missing ``after`` bundle, PNG-less screenshot, …)
    surfaces as the schema's enumerated :class:`RefusalError`, never a green
    bundle.

Record handling (mirrors ``emit.py``):
  * ``crawl_meta``  — two are emitted (start + terminal); the LAST wins.
    Top-level fields ``crawl_id / target_url / explorer_version /
    config_fingerprint`` seed the bundle header (``frame_count`` is recomputed
    honestly from the screenshots actually collected, never trusted).
  * ``page_state``  — one bundle page each.  Actions + screenshots may be
    EMBEDDED on the record (``page_state.actions`` / ``page_state.screenshots``)
    or, for actions, STREAMED as separate ``action`` records keyed by
    ``state_id``.  Embedded actions take precedence when present; otherwise the
    streamed records for that ``state_id`` are used (never both — a crawler
    uses one mechanism, and mixing would surface as a duplicate-subaction
    refusal).  Manifest-only routing fields (``state_id``, ``ax_fingerprint``,
    ``to_state``, ``screenshot_after``, ``phase``) are STRIPPED — the bundle
    models forbid extras, so leaking them would (correctly) fail validation.
  * ``guard_event`` / ``edge`` — audit-only, ignored by the mapper.
"""
from __future__ import annotations

import base64
from typing import Any, Callable, Iterable

from pydantic import ValidationError

from ..substrate.schema import (
    ExplorationBundle,
    RefusalError,
    _refusal_from_validation_error,
)

# ─── Manifest vocabulary (pinned to engines/qe-explorer/app/emit.py) ─────────

#: ``type`` discriminators.
REC_CRAWL_META = "crawl_meta"
REC_PAGE_STATE = "page_state"
REC_ACTION = "action"
REC_GUARD_EVENT = "guard_event"
REC_EDGE = "edge"

#: crawl_meta top-level fields the mapper reads (frame_count is RECOMPUTED).
CRAWL_META_FIELDS = ("crawl_id", "target_url", "explorer_version", "config_fingerprint")

#: page_state fields copied into a bundle ``PageState`` (schema field names).
PAGE_STATE_SCHEMA_FIELDS = (
    "sequence_index", "location", "title", "url_host", "url_path", "url_query",
    "canonical_host", "first_seen_ms", "last_seen_ms", "form_snapshot",
    "form_snapshot_signals", "displayed_values", "network_calls",
)

#: action fields copied into a bundle ``ActionRecord`` (schema field names).
ACTION_SCHEMA_FIELDS = (
    "subaction_index", "verb", "target_label", "target_kind", "value",
    "timestamp_ms", "anchor", "after", "url_changed", "qec",
)

#: screenshot fields copied into a bundle ``ScreenshotRef`` (``path`` is
#: resolved by ``screenshot_loader`` into ``png_base64``).
SCREENSHOT_SCHEMA_FIELDS = ("frame_index", "timestamp_ms")

#: Manifest-only routing fields the mapper deliberately STRIPS.
MANIFEST_ONLY_PAGE_FIELDS = ("state_id", "ax_fingerprint")
MANIFEST_ONLY_ACTION_FIELDS = ("state_id", "to_state", "screenshot_after", "phase")


class ManifestMappingError(RefusalError):
    """A manifest is structurally unusable (missing crawl_meta, dangling
    ``state_id``, un-loadable screenshot, …).

    Subclasses :class:`RefusalError` so the router funnels it into the SAME
    honest 422 path as an evidence-rule refusal (``reason='schema_invalid'``
    unless a more specific enumerated reason is supplied).
    """

    def __init__(self, detail: str, reason: str = "schema_invalid") -> None:
        super().__init__(reason, detail)


def _require(record: dict, key: str, ctx: str) -> Any:
    if key not in record:
        raise ManifestMappingError(f"{ctx} is missing required field {key!r}")
    return record[key]


def _clean_action(raw: dict) -> dict:
    """Project one manifest action dict onto the schema ``ActionRecord`` fields.

    Manifest-only routing keys are dropped; ``value`` stays ``None`` when the
    action committed no value (a click), which the schema permits for non-fill
    verbs and refuses for ``type``/``select``.
    """
    return {
        "subaction_index": _require(raw, "subaction_index", "action"),
        "verb": _require(raw, "verb", "action"),
        "target_label": raw.get("target_label", ""),
        "target_kind": _require(raw, "target_kind", "action"),
        "value": raw.get("value"),
        "timestamp_ms": raw.get("timestamp_ms", 0),
        "anchor": raw.get("anchor"),
        "after": raw.get("after"),
        "url_changed": bool(raw.get("url_changed", False)),
        "qec": dict(raw.get("qec") or {}),
    }


def _clean_screenshot(raw: dict, loader: Callable[[str], bytes]) -> dict:
    """Project one manifest screenshot dict onto the schema ``ScreenshotRef``.

    The staged PNG (referenced by ``path``, design R-5) is read via the
    injected ``loader`` and base64-encoded; the schema then verifies it is a
    real PNG.  A missing/empty/un-loadable file is an honest refusal.
    """
    frame_index = _require(raw, "frame_index", "screenshot")
    timestamp_ms = _require(raw, "timestamp_ms", "screenshot")
    path = str(_require(raw, "path", "screenshot"))
    try:
        data = loader(path)
    except FileNotFoundError as exc:
        raise ManifestMappingError(
            f"staged screenshot {path!r} (frame {frame_index}) not found: {exc}",
            reason="screenshot_missing_data",
        ) from exc
    except Exception as exc:  # loader failure is an honest, loud refusal
        raise ManifestMappingError(
            f"staged screenshot {path!r} (frame {frame_index}) unreadable: {exc}",
            reason="screenshot_missing_data",
        ) from exc
    if not data:
        raise ManifestMappingError(
            f"staged screenshot {path!r} (frame {frame_index}) is empty",
            reason="screenshot_missing_data",
        )
    return {
        "frame_index": frame_index,
        "timestamp_ms": timestamp_ms,
        "png_base64": base64.b64encode(bytes(data)).decode("ascii"),
    }


def _last_crawl_meta(records: list[dict]) -> dict:
    metas = [r for r in records if r.get("type") == REC_CRAWL_META]
    if not metas:
        raise ManifestMappingError("manifest carries no crawl_meta record")
    meta = metas[-1]
    missing = [f for f in CRAWL_META_FIELDS if not str(meta.get(f) or "").strip()]
    if missing:
        raise ManifestMappingError(
            f"crawl_meta is missing required field(s): {missing}"
        )
    return meta


def map_manifest_records_to_bundle(
    records: Iterable[dict],
    *,
    screenshot_loader: Callable[[str], bytes],
) -> ExplorationBundle:
    """Map parsed explorer manifest records → a validated ``ExplorationBundle``.

    Args:
        records:           the parsed ``manifest.jsonl`` lines (dicts), in
                           emission order.  Page order is PRESERVED (never
                           re-sorted) so a non-monotonic emission is caught by
                           the schema rather than silently normalised.
        screenshot_loader: ``path -> PNG bytes`` for a staged screenshot
                           (design R-5).  Injected so the mapper is pure and
                           the contract test needs no filesystem.

    Returns:
        A schema-validated :class:`ExplorationBundle` — the exact shape the
        Phase-0 writer persists.

    Raises:
        RefusalError:  a structural problem (:class:`ManifestMappingError`) or
                       any evidence-rule violation (raised by the bundle
                       validators, converted to an enumerated reason).
    """
    records = list(records)
    meta = _last_crawl_meta(records)

    # Stream actions keyed by state_id (used only when a page embeds none).
    streamed_by_state: dict[str, list[dict]] = {}
    for rec in records:
        if rec.get("type") == REC_ACTION:
            streamed_by_state.setdefault(str(rec.get("state_id") or ""), []).append(rec)

    pages: list[dict] = []
    total_shots = 0
    for rec in records:
        if rec.get("type") != REC_PAGE_STATE:
            continue
        state_id = str(rec.get("state_id") or "")

        embedded_actions = list(rec.get("actions") or [])
        raw_actions = embedded_actions if embedded_actions else streamed_by_state.get(state_id, [])
        # Deterministic order by the authoritative subaction_index; a genuine
        # duplicate is left for the schema to refuse honestly.
        cleaned_actions = [
            _clean_action(a)
            for a in sorted(raw_actions, key=lambda a: a.get("subaction_index", 0))
        ]

        cleaned_shots = [_clean_screenshot(s, screenshot_loader) for s in (rec.get("screenshots") or [])]
        total_shots += len(cleaned_shots)

        pages.append({
            "sequence_index": _require(rec, "sequence_index", "page_state"),
            "location": _require(rec, "location", "page_state"),
            "title": rec.get("title", ""),
            "url_host": rec.get("url_host", ""),
            "url_path": rec.get("url_path", ""),
            "url_query": rec.get("url_query", ""),
            "canonical_host": rec.get("canonical_host", ""),
            "first_seen_ms": _require(rec, "first_seen_ms", "page_state"),
            "last_seen_ms": _require(rec, "last_seen_ms", "page_state"),
            "form_snapshot": dict(rec.get("form_snapshot") or {}),
            "form_snapshot_signals": dict(rec.get("form_snapshot_signals") or {}),
            "displayed_values": list(rec.get("displayed_values") or []),
            "network_calls": list(rec.get("network_calls") or []),
            "actions": cleaned_actions,
            "screenshots": cleaned_shots,
        })

    bundle_dict = {
        "crawl_id": str(meta["crawl_id"]),
        "target_url": str(meta["target_url"]),
        "explorer_version": str(meta["explorer_version"]),
        "config_fingerprint": str(meta["config_fingerprint"]),
        # RECOMPUTED honestly — never trust a manifest-declared count.
        "frame_count": total_shots,
        "pages": pages,
    }
    try:
        return ExplorationBundle.model_validate(bundle_dict)
    except ValidationError as exc:
        # Convert pydantic-wrapped RefusalErrors back to the ONE honest type.
        raise _refusal_from_validation_error(exc) from exc

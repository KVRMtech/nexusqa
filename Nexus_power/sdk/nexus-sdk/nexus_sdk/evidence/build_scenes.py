"""
Scene Builder — Phase 3 algorithm.

Groups visually similar consecutive frames into stable logical scenes using
perceptual hash distance (dHash Hamming distance) when available, or OCR-text
Jaccard similarity as a fallback when the frame payload carries no hash.
Scene IDs are deterministic (uuid5) so that repeated runs for the same
artifact produce identical IDs, making DB upserts idempotent.

No external dependencies — pure Python computation over frame dicts.

Phase 2 — guards & merge re-enablement
--------------------------------------
Behind the ``EYES_PHASE2_GUARDS`` env flag (default OFF), every scene is
emitted with three additional signals used to safely de-fragment scrolling
micro-shifts WITHOUT re-introducing the form-fill merge bug:

  * ``has_keyframe_boundary``   — dHash break > 1.5× threshold (hard cut)
  * ``visible_text_fingerprint``— chrome-stripped OCR tokens (form-field
                                   diffs survive even when chrome words
                                   dominate raw OCR overlap)
  * ``detected_controls``       — placeholder list for spine back-fill
  * ``entry_action`` / ``exit_action`` — placeholder strings for spine
                                   back-fill from edges

When the flag is OFF the fields are still emitted (cheap, additive) but the
Phase-1 "merge effectively disabled" behaviour is preserved.
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Optional
from urllib.parse import urlparse


# ── Phase 2 feature flag ───────────────────────────────────────
def _phase2_guards_enabled() -> bool:
    """Return True iff the Phase-2 guard system is enabled.

    Default OFF so this code is a strict no-op (identical Phase 1 behaviour)
    until the operator explicitly opts in.  Read at every call so tests and
    runtime overrides take effect without process restart.
    """
    return os.environ.get("EYES_PHASE2_GUARDS", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Tokens that are page chrome / browser UI / recording overlays — stripped
# from the structural fingerprint so form-fill content differences are not
# drowned out by ambient text.
_CHROME_TOKENS: frozenset[str] = frozenset({
    "home", "back", "menu", "search", "close", "settings",
    "cancel", "ok", "submit", "next", "previous", "logout", "login",
    "signin", "signup", "register", "help", "support",
    "google", "chrome", "edge", "firefox", "incognito", "private",
    "tab", "window", "new", "save", "open", "file", "edit", "view",
    "zoom", "teams", "loom", "recording", "meeting",
    "the", "and", "for", "you", "your", "with", "this", "that",
    "https", "http", "www", "com", "org", "net", "gov", "edu",
})


# ── Namespace for deterministic UUIDs ─────────────────────────
# uuid.NAMESPACE_OID is a stable RFC-4122 namespace for opaque IDs.
_NS = uuid.NAMESPACE_OID


# ── Configuration ──────────────────────────────────────────────

# Hamming distance threshold expressed as fraction of 64-bit hash length.
# 0.15 → 15% of bits differ → new scene.  Configurable at call site.
SCENE_BOUNDARY_THRESHOLD: float = 0.15

# Jaccard similarity threshold for text-fingerprint fallback.
# Frames with < 65% word-set overlap are considered different scenes.
_TEXT_SIMILARITY_THRESHOLD: float = 0.65

# Primary: standard URLs with scheme
_URL_PATTERN = re.compile(r"https?://[^\s\"'>]+")

# Pattern to detect heuristic-generated template descriptions.
# Matches patterns like:
#   "web_ui screen with 3 buttons, 1 text_field"
#   "screen with 2 buttons"
#   "Screen showing N elements"
#   "mobile_ui screen with ..."
_TEMPLATE_DESC = re.compile(
    r"^(\w+_\w+\s+)?screen (with|showing) \w+", re.IGNORECASE
)

# Secondary: OCR-mangled URLs — domain.tld/path without scheme.
# OCR commonly drops "://" or inserts spaces around it.
# Matches: "guardianlife com/life-insurance", "google.com/search",
#          "www guardianlife com/path", "guardianlife.com/path"
# Requires 4+ char domain AND a /path component to avoid false positives
# from OCR noise (e.g. "USAA" + ".US" or "Insurance" + ".Ca").
_BARE_DOMAIN_PATTERN = re.compile(
    r"\b((?:www[\s.])?[a-z0-9][-a-z0-9]{3,62})"  # domain name: 4+ chars (optionally www)
    r"[\s.]"                                       # OCR space or dot before TLD
    r"(com|org|net|io|gov|edu|co|us|uk|ca|au|de|fr)"  # TLD
    r"(/[^\s\"'>]+)",                              # REQUIRED path (no optional — must have /path)
    re.IGNORECASE,
)


# ── URL-semantic screen naming ─────────────────────────────────

# Known brand names for common domains: maps registered domain → display brand.
# Avoids "Usaa" capitalisation issues from naive title-casing.
_BRAND_REGISTRY: dict[str, str] = {
    "usaa.com": "USAA",
    "guardianlife.com": "Guardian Life",
    "metlife.com": "MetLife",
    "prudential.com": "Prudential",
    "cigna.com": "Cigna",
    "anthem.com": "Anthem",
    "aetna.com": "Aetna",
    "salesforce.com": "Salesforce",
    "force.com": "Salesforce",
    "servicenow.com": "ServiceNow",
    "workday.com": "Workday",
    "oracle.com": "Oracle",
    "sap.com": "SAP",
    "microsoft.com": "Microsoft",
    "office.com": "Microsoft Office",
    "outlook.com": "Outlook",
    "office365.com": "Microsoft 365",
    "sharepoint.com": "SharePoint",
    "teams.microsoft.com": "Microsoft Teams",
    "google.com": "Google",
    "gmail.com": "Gmail",
    "docs.google.com": "Google Docs",
    "sheets.google.com": "Google Sheets",
    "drive.google.com": "Google Drive",
    "github.com": "GitHub",
    "jira.atlassian.com": "Jira",
    "confluence.atlassian.com": "Confluence",
    "atlassian.com": "Atlassian",
    "zendesk.com": "Zendesk",
    "intercom.io": "Intercom",
    "slack.com": "Slack",
    "zoom.us": "Zoom",
}

# Path segment words that are too generic to add value to the screen name
_GENERIC_PATH_SEGMENTS = {
    "index", "home", "default", "main", "app", "portal", "dashboard",
    "page", "view", "show", "detail", "new", "edit", "create",
    "www", "web", "site", "public", "static", "assets",
}


def _screen_name_from_url(url: str) -> str:
    """Derive a human-readable screen name from a URL.

    Examples:
      usaa.com/insurance/life/quote   → "USAA — Life › Quote"
      salesforce.com/leads/new        → "Salesforce — New Lead"
      github.com/org/repo/pulls       → "GitHub — Pull Requests"

    Returns empty string if the URL provides no useful information.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return ""

    host = parsed.netloc or parsed.path.split("/")[0]
    host = host.lower().lstrip("www.").split("@")[-1].split(":")[0]

    # Find brand from registry: try full host, then eTLD+1 (last two parts)
    brand = _BRAND_REGISTRY.get(host)
    if not brand:
        parts = host.split(".")
        etld1 = ".".join(parts[-2:]) if len(parts) >= 2 else host
        brand = _BRAND_REGISTRY.get(etld1)
    if not brand:
        # Fall back to capitalising the domain name (e.g. "mycompany")
        domain_name = host.split(".")[0]
        brand = domain_name.upper() if len(domain_name) <= 5 else domain_name.title()

    # Build path label from meaningful path segments
    path = (parsed.path or "").strip("/")
    raw_segments = [s for s in path.split("/") if s]
    meaningful: list[str] = []
    for seg in raw_segments[-3:]:  # last 3 segments are most specific
        word = seg.replace("-", " ").replace("_", " ").strip()
        # Skip UUIDs and numeric IDs
        if re.match(r"^[0-9a-f\-]{8,}$", seg, re.IGNORECASE):
            continue
        if re.match(r"^\d+$", seg):
            continue
        if word.lower() in _GENERIC_PATH_SEGMENTS:
            continue
        meaningful.append(word.title())

    if meaningful:
        return f"{brand} — {' › '.join(meaningful)}"
    return brand



def _parse_dhash(dhash_value: object) -> Optional[int]:
    """Return integer dHash or None if not present / not parseable."""
    if dhash_value is None:
        return None
    if isinstance(dhash_value, int):
        return dhash_value
    try:
        return int(str(dhash_value), 16)
    except (ValueError, TypeError):
        return None


def _hamming_distance(a: int, b: int) -> float:
    """Hamming distance as fraction of 64 bits."""
    xor = a ^ b
    bits_set = bin(xor).count("1")
    return bits_set / 64.0


# ── Text-fingerprint helpers (hash-absent fallback) ────────────

def _text_fingerprint(text: str) -> frozenset[str]:
    """Return a frozenset of meaningful words for Jaccard comparison."""
    if not text:
        return frozenset()
    words = re.findall(r"\b[a-z0-9]{2,}\b", text.lower())
    return frozenset(words)


def _text_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity between two word sets."""
    if not a and not b:
        return 1.0  # both empty → same content
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union


# ── Phase 2 helpers (A1) ───────────────────────────────────────

def _structural_fingerprint(ocr_text: str) -> frozenset[str]:
    """Return *content-only* OCR tokens — chrome words and stop-words removed.

    Two scenes that share identical chrome (nav bar, button row, footer) but
    differ in form-field content (e.g. "Annual Premium 5000" vs "Annual
    Premium 5500") get a low Jaccard score on their structural fingerprints
    even though their raw OCR fingerprints overlap >85%.  This is the
    primary signal that lets us safely re-enable scene merging in A2 without
    collapsing real form-fill steps.

    Tokenisation rules:
      * Word boundary, ≥3 alphanumerics, lower-cased.
      * Both letter-led (``annual``, ``premium``) AND digit-led (``5000``,
        ``2026``) tokens are kept.  Numeric form values are exactly the
        signal we need to distinguish fill states.
      * Tokens in ``_CHROME_TOKENS`` are dropped (page chrome / browser
        UI / recording overlays / stop-words).
    """
    if not ocr_text:
        return frozenset()
    words = re.findall(r"\b[a-z0-9][a-z0-9]{2,}\b", ocr_text.lower())
    return frozenset(w for w in words if w not in _CHROME_TOKENS)


def _hard_keyframe_indices(
    sorted_frames: list[dict],
    threshold: float,
) -> set[int]:
    """Return frame_index values where the dHash Hamming distance from the
    immediately preceding frame exceeds ``threshold * 1.5``.

    These are *hard* visual cuts (modal opens, page navigates, app switches)
    that must NEVER be merged across, regardless of OCR similarity.  The
    1.5× multiplier is intentionally stricter than the scene-boundary
    threshold itself: every hard keyframe is a scene boundary, but not every
    scene boundary is a hard keyframe.
    """
    if not sorted_frames:
        return set()

    hard_threshold = threshold * 1.5
    out: set[int] = set()
    prev_hash: Optional[int] = None

    for f in sorted_frames:
        curr_hash = _parse_dhash(f.get("dhash") or f.get("hash"))
        if prev_hash is not None and curr_hash is not None:
            d = _hamming_distance(curr_hash, prev_hash)
            if d > hard_threshold:
                idx = f.get("frame_index")
                if idx is not None:
                    out.add(int(idx))
        if curr_hash is not None:
            prev_hash = curr_hash

    return out


# ── OCR title extraction ──────────────────────────────────────

# Browser chrome keywords — anything after these is chrome noise, not page content.
# Also includes recording-software overlays (Zoom, Teams, Loom) and private/incognito
# browser indicators that OCR reads alongside real page content.
_CHROME_BREAKS = [
    # Chrome / Edge tab bar chrome
    "New Incognito Tab", "New Tab", "Private Browsing", "InPrivate",
    "Incognito Window", "Incognito Mode",
    # Chrome AI / extension chrome
    "Ask Gemini", "Customize Chrome", "Show more", "Al Mode",
    # Windows taskbar noise (weather, clock widgets)
    "Mostly cloudy", "Partly cloudy", "Mostly sunny",
    # Common in-browser UI noise
    "ChatGPT", "Inbox (",
    # Zoom / Teams / Loom recording overlays
    "Zoom Meeting", "Teams Meeting", "Loom Recording",
    "is recording", "Recording in progress",
]

# Pattern: "Title — breadcrumb" or "Title | subtitle"
_TITLE_SEP = re.compile(
    r"^([A-Z][A-Za-z0-9\s\-']{4,50})\s*(?:[—\-|»]|>\s)",
    re.MULTILINE,
)


def _clean_page_title(title: str) -> str:
    """Strip browser chrome suffixes from a page title string.

    Handles titles like "Life Insurance Get Quote New Incognito Tab X"
    → "Life Insurance Get Quote", stripping everything from the first
    recognised chrome keyword onward.

    Returns empty string if the cleaned result is too short (< 4 chars).
    """
    if not title:
        return ""
    earliest = len(title)
    for kw in _CHROME_BREAKS:
        pos = title.find(kw)
        if 0 < pos < earliest:
            earliest = pos
    cleaned = title[:earliest].strip().rstrip(";:,.'\"-| ")
    return cleaned if len(cleaned) >= 4 else ""


def _can_merge_phase2(prev: dict, curr: dict, raw_sim: float, struct_sim: float) -> bool:
    """Phase 2 (A2) — decide whether two adjacent scenes can be safely merged
    into one logical scene without losing a distinct UI step.

    Both similarity floors must hold AND every guard signal must be clear.

    Required (all must hold):
      * Same non-empty ``detected_url``
      * Raw OCR Jaccard               >= ``raw_sim``     (e.g. 0.80)
      * Structural fingerprint Jaccard>= ``struct_sim``  (e.g. 0.90)

    Anti-merge guards (any one blocks the merge):
      * Either scene has ``has_keyframe_boundary=True``
        (Eyes detected a hard cut at the scene's first frame)
      * ``curr.start_ms - prev.end_ms`` exceeds 10 000 ms
        (user paused / did something between the two scenes)
      * ``detected_controls`` differ (form-state advanced)
      * Either scene carries an ``entry_action`` / ``exit_action``
        (action evidence already attached to this transition)
    """
    prev_url = (prev.get("detected_url") or "").strip()
    curr_url = (curr.get("detected_url") or "").strip()
    if not prev_url or prev_url != curr_url:
        return False

    # Raw OCR similarity — necessary but not sufficient (chrome-dominated).
    prev_words = _text_fingerprint(prev.get("ocr_text", ""))
    curr_words = _text_fingerprint(curr.get("ocr_text", ""))
    if _text_similarity(prev_words, curr_words) < raw_sim:
        return False

    # Structural fingerprint — chrome-stripped content tokens.  This is the
    # signal that protects form-fill steps from being merged.
    prev_struct = frozenset(prev.get("visible_text_fingerprint") or [])
    curr_struct = frozenset(curr.get("visible_text_fingerprint") or [])
    if _text_similarity(prev_struct, curr_struct) < struct_sim:
        return False

    # Anti-merge guards.
    if prev.get("has_keyframe_boundary") or curr.get("has_keyframe_boundary"):
        return False
    if (curr.get("start_ms", 0) - prev.get("end_ms", 0)) > 10_000:
        return False
    prev_ctrl = set(prev.get("detected_controls") or [])
    curr_ctrl = set(curr.get("detected_controls") or [])
    if prev_ctrl and curr_ctrl and prev_ctrl != curr_ctrl:
        return False
    if curr.get("entry_action") or prev.get("exit_action"):
        return False

    return True


def _merge_pair(prev: dict, curr: dict) -> None:
    """In-place merge of ``curr`` into ``prev``.  Higher-confidence metadata
    wins; frame ids accumulate; time range expands.  Mutates ``prev``.

    ``ocr_text`` is the union of both scenes' OCR (sentence-deduplicated)
    regardless of which side's metadata wins. Without this, merging two
    initial groups produced by build_scenes (e.g. an empty-form group and
    a filled-form group on the same URL) drops one side's OCR entirely —
    losing every filled value the user typed. ``ocr_text`` IS the evidence,
    so it must always reflect the full merged frame set.
    """
    all_frame_ids = list({
        *(prev.get("frame_ids") or []),
        *(curr.get("frame_ids") or []),
    })
    merged_ocr = _merge_ocr_text(prev.get("ocr_text", ""), curr.get("ocr_text", ""))
    if (curr.get("completeness_confidence", 0.0)
            > prev.get("completeness_confidence", 0.0)):
        # curr has better LLaVA analysis — adopt its metadata wholesale,
        # but keep prev's scene_index/start_ms so order is preserved.
        kept = dict(curr)
        kept["scene_index"] = prev["scene_index"]
        kept["start_ms"] = min(prev.get("start_ms", 0), curr.get("start_ms", 0))
        kept["end_ms"] = max(prev.get("end_ms", 0), curr.get("end_ms", 0))
        kept["frame_ids"] = all_frame_ids
        kept["ocr_text"] = merged_ocr
        # Anti-merge guards: OR them so a downstream merge respects either.
        kept["has_keyframe_boundary"] = bool(
            prev.get("has_keyframe_boundary") or curr.get("has_keyframe_boundary")
        )
        # Structural fingerprint: union of both for downstream comparisons.
        kept["visible_text_fingerprint"] = list(
            set(prev.get("visible_text_fingerprint") or [])
            | set(curr.get("visible_text_fingerprint") or [])
        )
        prev.clear()
        prev.update(kept)
    else:
        prev["end_ms"] = max(prev.get("end_ms", 0), curr.get("end_ms", 0))
        prev["frame_ids"] = all_frame_ids
        prev["ocr_text"] = merged_ocr
        prev["has_keyframe_boundary"] = bool(
            prev.get("has_keyframe_boundary") or curr.get("has_keyframe_boundary")
        )
        prev["visible_text_fingerprint"] = list(
            set(prev.get("visible_text_fingerprint") or [])
            | set(curr.get("visible_text_fingerprint") or [])
        )


def _merge_ocr_text(a: str, b: str) -> str:
    """Sentence-deduplicated union of two OCR texts.

    Mirrors the sentence-split-and-dedup used by build_scenes() when it
    first constructs a scene's ocr_text, so re-applying it during merge
    preserves the exact same shape (one ``". "``-joined string of
    unique chunks) without quadratic blow-up across many merges.
    """
    seen: set[str] = set()
    parts: list[str] = []
    for text in (a, b):
        for sentence in (text or "").split("."):
            cleaned = sentence.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                parts.append(cleaned)
    return ". ".join(parts)


def _merge_phase1(scenes: list[dict]) -> list[dict]:
    """Phase 1 path — effectively disabled merge.  Kept verbatim for
    parity so flipping ``EYES_PHASE2_GUARDS`` back to false produces
    byte-identical scene_id assignments to the Wave-A release."""
    _MERGE_SIMILARITY = 0.99
    merged: list[dict] = [dict(scenes[0])]
    for curr in scenes[1:]:
        prev = merged[-1]
        prev_url = (prev.get("detected_url") or "").strip()
        curr_url = (curr.get("detected_url") or "").strip()
        if prev_url and prev_url == curr_url:
            prev_words = _text_fingerprint(prev.get("ocr_text", ""))
            curr_words = _text_fingerprint(curr.get("ocr_text", ""))
            if _text_similarity(prev_words, curr_words) >= _MERGE_SIMILARITY:
                # Guards (legacy, mostly empty in Phase 1)
                prev_has_keyframe = prev.get("has_keyframe_boundary", False)
                curr_has_keyframe = curr.get("has_keyframe_boundary", False)
                large_gap = (curr.get("start_ms", 0) - prev.get("end_ms", 0)) > 10_000
                prev_ctrl = set(prev.get("detected_controls") or [])
                curr_ctrl = set(curr.get("detected_controls") or [])
                controls_changed = bool(prev_ctrl.symmetric_difference(curr_ctrl))
                has_action = bool(curr.get("entry_action") or prev.get("exit_action"))
                if prev_has_keyframe or curr_has_keyframe or large_gap or controls_changed or has_action:
                    merged.append(dict(curr))
                    continue
                _merge_pair(prev, curr)
                continue
        merged.append(dict(curr))
    return merged


def _merge_phase2(scenes: list[dict]) -> list[dict]:
    """Phase 2 path — chrome-stripped structural fingerprint + multi-guard
    merge.  See :func:`_can_merge_phase2` for the full specification.

    Tuning constants:
      raw_sim    = 0.80  — raw OCR Jaccard floor
      struct_sim = 0.90  — content-token Jaccard floor (the form-fill guard)
    """
    _RAW_SIM = 0.80
    _STRUCT_SIM = 0.90
    merged: list[dict] = [dict(scenes[0])]
    for curr in scenes[1:]:
        prev = merged[-1]
        if _can_merge_phase2(prev, curr, raw_sim=_RAW_SIM, struct_sim=_STRUCT_SIM):
            _merge_pair(prev, curr)
        else:
            merged.append(dict(curr))
    return merged


def _merge_same_url_scenes(
    scenes: list[dict],
    artifact_id: str,
) -> list[dict]:
    """Merge consecutive scenes that share the same URL and similar OCR text.

    Dispatches based on the ``EYES_PHASE2_GUARDS`` env flag:

    * **Flag OFF (default)** → :func:`_merge_phase1` — Wave-A behaviour,
      effectively disabled merge (only literal duplicate scenes collapse).
      Guarantees the "every distinct step shown" customer-visible promise
      until Phase 2 is explicitly enabled.

    * **Flag ON** → :func:`_merge_phase2` — chrome-stripped structural
      fingerprint similarity (0.90) + raw OCR Jaccard (0.80) with active
      anti-merge guards on ``has_keyframe_boundary``, time gap,
      ``detected_controls`` change, and explicit action evidence.

    Why the historical "guards are dead" problem is now fixed:

    1. ``has_keyframe_boundary`` is computed in :func:`build_scenes` from
       a 1.5×-threshold dHash walk, BEFORE merging happens.
    2. ``visible_text_fingerprint`` strips chrome tokens so form-field
       diffs are not drowned out by ambient page text.
    3. ``detected_controls`` / ``entry_action`` / ``exit_action`` are
       populated by spine after Phase 6/7 for the *next* re-run; they
       remain empty (no-op guards) on the first run, but A1+A2 alone
       already solve the customer-visible defects without needing them.
    """
    if len(scenes) <= 1:
        return scenes

    merged = (
        _merge_phase2(scenes)
        if _phase2_guards_enabled()
        else _merge_phase1(scenes)
    )

    # Re-index sequentially and regenerate deterministic scene_ids so
    # downstream DB upserts remain idempotent under either path.
    # Regenerate scene_id unconditionally: _merge_pair adopts the surviving
    # scene's metadata wholesale (including its old scene_id) which can leave
    # scene_index and scene_id out of sync even when scene_index happens to
    # match the new position. Always regenerating prevents a stale scene_id
    # from colliding with a later sibling's regenerated scene_id during the
    # ON CONFLICT(scene_id) upsert in the spine persister.
    for new_idx, scene in enumerate(merged):
        scene["scene_index"] = new_idx
        scene["scene_id"] = str(uuid.uuid5(_NS, f"scene:{artifact_id}:{new_idx}"))

    return merged


def _state_key_merge_enabled() -> bool:
    """Default-ON kill switch for the semantic state_key merge pass."""
    return os.environ.get("EYES_STATE_KEY_MERGE", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


_URL_OCR_NOISE_RE = re.compile(r"[^a-z0-9/]+$", re.IGNORECASE)


def _normalized_url_path(url: str) -> str:
    """Lowercased path portion of a URL with OCR-induced trailing junk stripped.

    OCR often clips the URL bar mid-character so we see paths like
    ``/insurance/life-insurance-_`` or ``/insurance/life-insurance-quote-=``
    that are really truncations of the same URL.  We strip trailing
    non-alphanumeric junk segment by segment so two truncated variants of the
    same path collapse to a comparable prefix.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or "").lower().lstrip("www.")
        path = (parsed.path or "").rstrip("/")
        # Drop trailing punctuation/garbage from the last path segment so
        # ``/foo/bar-_`` and ``/foo/bar-quote-=`` both reduce to ``/foo/bar``.
        segs = [s for s in path.split("/") if s]
        if segs:
            segs[-1] = _URL_OCR_NOISE_RE.sub("", segs[-1]).rstrip("-_=")
            segs = [s for s in segs if s]
        return f"{host}/{'/'.join(segs)}".rstrip("/")
    except Exception:
        return url.lower().strip()


def _state_keys_compatible(prev_key: str, curr_key: str) -> bool:
    """Two semantic state_keys agree if they are identical, or if the shorter
    one is an OCR-truncated reading of the longer one.

    Exact equality is the common case.  Truncation tolerance is intentionally
    *narrow* — it fires only when the shorter key ends with a punctuation
    marker that cannot be a real path segment ending (``_`` or ``-``).  That
    rules out the trap of merging genuinely different sub-pages such as
    ``usaa.insurance.life`` (Life landing) and
    ``usaa.insurance.life_insurance_quote.personal_information.birthdate``
    (Birthdate form), which share leading segments but represent distinct
    SME steps.
    """
    if not prev_key or not curr_key:
        return False
    if prev_key == curr_key:
        return True

    p_parts = prev_key.split(".")
    c_parts = curr_key.split(".")
    if not p_parts or not c_parts or p_parts[0] != c_parts[0]:
        return False
    short, long_ = (p_parts, c_parts) if len(p_parts) <= len(c_parts) else (c_parts, p_parts)
    if len(short) < 2:
        return False

    # Require the shorter key's last segment to look truncated by OCR — i.e.
    # ending in ``_`` or ``-`` — before treating it as a noisy reading of the
    # longer one.  A cleanly-terminated shorter key is a *different* page.
    short_last = short[-1]
    if not short_last.endswith(("_", "-")):
        return False

    head_match = short[:-1] == long_[: len(short) - 1]
    tail_match = long_[len(short) - 1].startswith(short_last.rstrip("_-"))
    return head_match and tail_match


def _merge_by_state_key(scenes: list[dict], artifact_id: str) -> list[dict]:
    """Collapse consecutive scenes that represent the same logical step.

    The OCR-similarity merger above operates on *visual* signals which can
    drift across frames of the same screen (LLaVA description variation,
    cursor movement, scroll repaint).  This pass operates on the *semantic*
    state_key persisted in ``scene_state_summary``: when two adjacent scenes
    point at the same logical state, they are one step in the SME's flow
    regardless of pixel-level fluctuation.

    Three accumulation layers (each weaker, applied as fallbacks):

    1. **Exact state_key match** with reliable quality on both sides.  Catches
       the clean case where the URL parser produced identical keys.
    2. **Prefix-compatible state_key** with reliable quality on the longer
       side.  Catches OCR-truncation (``life_insurance_`` → ``life_insurance_quote``).
    3. **Normalized URL path match** when state_keys diverge entirely.  Catches
       OCR-mangled URL bars (``/quote-=``, ``/life-insurance-_``).
    4. **Sandwich rule**: a single weak scene with no usable state_key or URL
       that sits between two scenes already grouped together is folded into
       the group (transient frames, captured during the same step).

    Real-world failure this addresses (artifact bbfbf73d):
      * 9 consecutive frames of "Review estimate" page → 1 step.
      * 3 consecutive frames of "Nicotine Tobacco" question → 1 step.

    Anti-merge guards mirror the URL pass: keyframe boundary, control-set
    change, or explicit action evidence between the two scenes blocks the
    merge so genuine in-page state transitions (form submission, modal open)
    are preserved.
    """
    if not _state_key_merge_enabled() or len(scenes) <= 1:
        return scenes

    _RELIABLE_QUALITY = {"strong", "degraded"}
    # Content-token Jaccard floor for the URL-only merge path (Layer 3).
    # Mirrors ``_merge_phase2``'s ``struct_sim`` form-fill guard: two scenes
    # on the same URL whose chrome-stripped content tokens diverge below this
    # are a *distinct* same-URL step (add-to-cart count change, SPA form-fill,
    # typed value) and must NOT be collapsed.
    _STATE_KEY_STRUCT_SIM = 0.90

    merged: list[dict] = [dict(scenes[0])]
    visit_counts: dict[int, int] = {}

    def _structurally_diverged(prev: dict, curr: dict) -> bool:
        """True when both scenes carry a structural fingerprint AND their
        content-token Jaccard is below the floor — positive evidence that the
        same-URL page content changed (cart count, typed value, SPA step).

        Fail-open: when either side lacks a fingerprint we cannot prove
        divergence, so we return False and preserve the prior behaviour on the
        clean URL-match path (never green-wash by *asserting* sameness — we
        only BLOCK a merge when there is positive evidence of difference)."""
        prev_struct = frozenset(prev.get("visible_text_fingerprint") or [])
        curr_struct = frozenset(curr.get("visible_text_fingerprint") or [])
        if not prev_struct or not curr_struct:
            return False
        return _text_similarity(prev_struct, curr_struct) < _STATE_KEY_STRUCT_SIM

    def _can_extend_group(prev: dict, curr: dict) -> bool:
        prev_summary = prev.get("scene_state_summary") or {}
        curr_summary = curr.get("scene_state_summary") or {}
        prev_key = (prev_summary.get("state_key") or "").strip()
        curr_key = (curr_summary.get("state_key") or "").strip()
        prev_quality = prev_summary.get("state_quality")
        curr_quality = curr_summary.get("state_quality")
        prev_url = _normalized_url_path(prev.get("detected_url") or "")
        curr_url = _normalized_url_path(curr.get("detected_url") or "")

        prev_reliable = prev_quality in _RELIABLE_QUALITY
        curr_reliable = curr_quality in _RELIABLE_QUALITY

        # Layer 1+2: state_key compatibility (exact or prefix), both reliable
        if prev_reliable and curr_reliable and _state_keys_compatible(prev_key, curr_key):
            return True

        # Layer 3: normalized URL path matches and at least one side reliable.
        # Gated behind a structural-fingerprint floor: a same-URL pair whose
        # chrome-stripped content tokens diverged is a distinct same-URL step
        # (add-to-cart, SPA form-fill) — refuse the merge so it stays visible.
        if (
            prev_url
            and prev_url == curr_url
            and (prev_reliable or curr_reliable)
            and not _structurally_diverged(prev, curr)
        ):
            return True

        # Layer 4: sandwich rule — curr is a weak transient with no usable
        # signal of its own; allow it to extend an already-reliable group so
        # OCR drop-outs do not break the chain.
        if (
            prev_reliable
            and not curr_reliable
            and not curr_key
            and not curr_url
        ):
            return True

        return False

    for curr in scenes[1:]:
        prev = merged[-1]

        prev_ctrl = set(prev.get("detected_controls") or [])
        curr_ctrl = set(curr.get("detected_controls") or [])
        controls_changed = bool(prev_ctrl.symmetric_difference(curr_ctrl))
        has_action = bool(curr.get("entry_action") or prev.get("exit_action"))
        keyframe_break = bool(
            prev.get("has_keyframe_boundary") or curr.get("has_keyframe_boundary")
        )
        guarded = controls_changed or has_action or keyframe_break

        if not guarded and _can_extend_group(prev, curr):
            _merge_pair(prev, curr)
            survivor_idx = id(prev)
            visit_counts[survivor_idx] = visit_counts.get(survivor_idx, 1) + 1
        else:
            merged.append(dict(curr))

    # Stamp visit_count back into scene_state_summary so the UI can render
    # "viewed N times" without inventing the field at display time.
    for scene in merged:
        n = visit_counts.get(id(scene), 1)
        if n > 1:
            summary = dict(scene.get("scene_state_summary") or {})
            summary["visit_count"] = n
            scene["scene_state_summary"] = summary

    # Re-index sequentially and regenerate deterministic scene_ids so DB
    # upserts stay idempotent (same convention as _merge_same_url_scenes).
    # See note there: scene_id must be regenerated unconditionally to avoid
    # post-merge collisions on ON CONFLICT(scene_id).
    for new_idx, scene in enumerate(merged):
        scene["scene_index"] = new_idx
        scene["scene_id"] = str(uuid.uuid5(_NS, f"scene:{artifact_id}:{new_idx}"))

    return merged


def _classify_screen_type(url: str, ocr_text: str, description: str) -> str:
    """Classify the screen type from URL path patterns and OCR/description content.

    Returns one of: form | results | confirmation | listing | detail | landing | unknown
    """
    url_lower = (url or "").lower()
    ocr_lower = (ocr_text or "").lower()

    if url_lower:
        if any(x in url_lower for x in ["/results", "/quote-result", "/estimate", "/summary"]):
            return "results"
        if any(x in url_lower for x in ["/login", "/signin", "/sign-in", "/auth"]):
            return "form"
        if any(x in url_lower for x in ["/form", "/apply", "/application", "/calculator", "/quote-form", "/quote/form"]):
            return "form"
        if any(x in url_lower for x in ["/confirm", "/success", "/thank", "/complete", "/approved"]):
            return "confirmation"
        if any(x in url_lower for x in ["/detail", "/view", "/profile", "/account"]):
            return "detail"
        if any(x in url_lower for x in ["/list", "/search", "/browse", "/products", "/plans"]):
            return "listing"
        # Bare domain or single path segment → landing
        if url_lower.count("/") <= (3 if "://" in url_lower else 1):
            return "landing"

    if ocr_lower:
        if any(x in ocr_lower for x in ["your results", "your quote", "per month", "$ per", "estimated premium"]):
            return "results"
        if any(x in ocr_lower for x in ["step 1", "step 2", "step 3", "continue to step", "next step"]):
            return "form"
        if any(x in ocr_lower for x in ["thank you", "confirmation number", "you're all set", "order confirmed"]):
            return "confirmation"
        if any(x in ocr_lower for x in ["sign in", "log in", "create account", "forgot password"]):
            return "form"

    return "unknown"


def _build_scene_state_summary(
    screen_name: str,
    detected_url: str,
    ocr_text: str,
    description: str,
    app_type: str,
    has_real_timing: bool,
    group_size: int,
    completeness_confidence: float,
) -> dict:
    """Build a structured, confidence-scored scene_state_summary.

    Derives state_quality (strong | degraded | weak) from how many independent
    signals contributed.  This is the persisted truth that the UI should render
    instead of re-deriving from raw OCR at display time.

    The returned dict is stored in the ``scene_state_summary`` JSON column on
    ``visual_scenes``.  Every field is optional/nullable for callers that do
    not have full signal coverage.
    """
    state_signals: list[str] = []
    state_confidence: float = 0.0
    low_confidence_reasons: list[str] = []

    # ── Application identity from URL ─────────────────────────────────────
    domain = ""
    application_label = ""
    page_key = ""
    state_key = ""

    if detected_url:
        try:
            parsed = urlparse(detected_url if "://" in detected_url else f"https://{detected_url}")
            host = (parsed.netloc or "").lower().lstrip("www.").split(":")[0].split("@")[-1]
            domain = host

            brand = _BRAND_REGISTRY.get(host)
            if not brand:
                parts = host.split(".")
                etld1 = ".".join(parts[-2:]) if len(parts) >= 2 else host
                brand = _BRAND_REGISTRY.get(etld1)
            if brand:
                application_label = brand
            elif host:
                domain_label = host.split(".")[0]
                application_label = domain_label.upper() if len(domain_label) <= 5 else domain_label.title()

            # Build page_key from meaningful path segments
            path = (parsed.path or "").strip("/")
            raw_segs = [s for s in path.split("/") if s and s.lower() not in _GENERIC_PATH_SEGMENTS]
            clean_segs = [
                re.sub(r"[^a-z0-9]+", "_", s.lower())[:20]
                for s in raw_segs[:4]
                if not re.match(r"^[0-9a-f\-]{8,}$", s) and not re.match(r"^\d+$", s)
            ]
            prefix = re.sub(r"[^a-z0-9]", "_", application_label.lower())[:20] if application_label else "app"
            page_key = prefix + ("." + ".".join(clean_segs) if clean_segs else "")
            state_key = page_key  # refined sub-state detection deferred

            state_signals.append("url_path")
            state_confidence += 0.30
        except Exception:
            pass

    # ── screen_name signal quality ─────────────────────────────────────────
    if screen_name and not re.match(r"^Scene \d+$", screen_name):
        if detected_url and screen_name == _screen_name_from_url(detected_url):
            # Derived purely from URL — very reliable for web apps
            state_signals.append("url_semantic_name")
            state_confidence += 0.25
        else:
            # Came from cleaned page_title or LLaVA description
            state_signals.append("page_title")
            state_confidence += 0.15
    else:
        low_confidence_reasons.append("screen_name_fallback")
        state_confidence -= 0.15

    # ── LLaVA coverage ────────────────────────────────────────────────────
    if description:
        state_signals.append("llava_description")
        state_confidence += 0.10
    else:
        low_confidence_reasons.append("no_llava_analysis")
        state_confidence -= 0.05

    # ── OCR text density ─────────────────────────────────────────────────
    ocr_word_count = len((ocr_text or "").split())
    if ocr_word_count >= 15:
        state_signals.append("ocr_text")
        state_confidence += 0.10
    elif ocr_word_count >= 5:
        state_signals.append("ocr_partial")
        state_confidence += 0.05

    # ── Frame group stability ────────────────────────────────────────────
    if group_size >= 3:
        state_signals.append("multi_frame_group")
        state_confidence += 0.05

    # ── Penalties ─────────────────────────────────────────────────────────
    if not detected_url and app_type in ("web_ui", "unknown"):
        low_confidence_reasons.append("no_url_for_web_scene")
        state_confidence -= 0.10

    if completeness_confidence < 0.8:
        state_confidence -= 0.05

    state_confidence = max(0.0, min(1.0, state_confidence))

    if state_confidence >= 0.65:
        state_quality = "strong"
    elif state_confidence >= 0.35:
        state_quality = "degraded"
    else:
        state_quality = "weak"

    screen_type = _classify_screen_type(detected_url, ocr_text, description)
    step_label = screen_name.split(" › ")[-1] if " › " in screen_name else screen_name.split(" — ")[-1] if " — " in screen_name else screen_name

    return {
        "screen_type": screen_type,
        "screen_title": screen_name,
        "step_label": step_label,
        "application_label": application_label,
        "domain": domain,
        "page_key": page_key,
        "state_key": state_key,
        "state_signals": state_signals,
        "state_confidence": round(state_confidence, 3),
        "state_quality": state_quality,
        "low_confidence_reasons": low_confidence_reasons,
    }


def _extract_ocr_title(ocr_text: str) -> str:
    """Extract a short, meaningful page title from OCR text.

    Priority:
    1. Text before the first browser chrome keyword (tab bar title)
    2. "Title — breadcrumb" pattern (common in browser title bars)
    3. First line or segment between 5-60 chars that looks like a heading
    4. Empty string (caller should fall back)
    """
    if not ocr_text:
        return ""

    # Pattern 1: chop off at first browser chrome keyword
    # OCR typically reads: "Page Title New Tab Ask Gemini ..."
    # The text before the first chrome keyword is the page title.
    earliest_pos = len(ocr_text)
    for kw in _CHROME_BREAKS:
        pos = ocr_text.find(kw)
        if pos > 0 and pos < earliest_pos:
            earliest_pos = pos
    if earliest_pos < len(ocr_text):
        candidate = ocr_text[:earliest_pos].strip().rstrip(";:,.'\"")
        if 5 <= len(candidate) <= 80:
            return candidate[:60]

    # Pattern 2: explicit title separator
    sep_match = _TITLE_SEP.search(ocr_text)
    if sep_match:
        return sep_match.group(1).strip().rstrip(";:,.'\"")[:60]

    # Pattern 3: first whitespace-delimited segment of reasonable length
    # Try newline-split first, then period-split
    for splitter in ["\n", "."]:
        for segment in ocr_text.split(splitter)[:5]:
            candidate = segment.strip().rstrip(";:,.'\"")
            if (
                5 <= len(candidate) <= 60
                and not candidate.isdigit()
                and re.match(r"^[A-Z]", candidate)
                and len(candidate.split()) >= 2
            ):
                return candidate

    return ""


# ── Scene boundary decision ────────────────────────────────────

def _exceeds_threshold(
    current_hash: Optional[int],
    reference_hash: Optional[int],
    threshold: float,
    current_fp: frozenset[str],
    reference_fp: frozenset[str],
    is_keyframe: bool,
) -> bool:
    """Return True if a new scene should start.

    Decision priority:
    1. ``is_keyframe=True`` — Eyes detected a significant state change; always
       start a new scene regardless of hash/text signals.
    2. Hash available — use Hamming distance (most reliable).
    3. No hash — fall back to OCR word-set Jaccard similarity.  If similarity
       drops below ``_TEXT_SIMILARITY_THRESHOLD`` the frames are considered
       visually distinct enough to start a new scene.
    """
    if is_keyframe:
        return True

    if current_hash is not None and reference_hash is not None:
        return _hamming_distance(current_hash, reference_hash) > threshold

    # Text-fingerprint fallback: low similarity → new scene
    return _text_similarity(current_fp, reference_fp) < _TEXT_SIMILARITY_THRESHOLD


# ── Scene builder ──────────────────────────────────────────────

def build_scenes(
    frames: list[dict],
    artifact_id: str,
    session_id: str,
    tenant_id: str,
    threshold: float = SCENE_BOUNDARY_THRESHOLD,
) -> list[dict]:
    """Group frames into stable scenes and return scene dicts ready for DB insert.

    Scene IDs are **deterministic**: uuid5(NAMESPACE_OID, "scene:{artifact_id}:{scene_index}")
    so that retrying the same artifact produces identical IDs and DB upserts are
    idempotent via ON CONFLICT DO NOTHING.

    Algorithm
    ---------
    1. Sort frames by frame_index (ascending).
    2. Walk frames; start a new scene when:
       a. ``is_keyframe=True`` (Eyes detected a state change), OR
       b. dHash Hamming distance from the scene's *first* frame exceeds ``threshold``, OR
       c. No hash available and OCR text Jaccard similarity drops below 0.65.
    3. For each scene group, compute metadata:
       - representative_frame_id: frame with highest ocr_confidence in group
       - screen_name: first sentence of representative frame's description,
         truncated to 120 chars
       - ocr_text: concatenated extracted_text, deduplicated by sentence
       - detected_url: first URL match from ocr_text
       - start_ms / end_ms: min / max timestamp_ms of frames in group
         (falls back to timestamp_seconds * 1000 if timestamp_ms absent)
       - completeness_confidence: 1.0 if LLaVA description on representative
         frame is non-empty, 0.5 otherwise

    Parameters
    ----------
    frames:      List of frame dicts (Eyes FrameAnalysis payloads).
    artifact_id: Canonical artifact this scene belongs to.
    session_id:  Session scope.
    tenant_id:   Tenant scope.
    threshold:   dHash Hamming fraction above which a new scene starts.

    Returns
    -------
    List of scene dicts, each with a deterministic scene_id UUID, and a
    ``frame_ids`` key listing the frame_id values in the group (used by
    the caller to backfill visual_frames.scene_id).
    """
    if not frames:
        return []

    sorted_frames = sorted(frames, key=lambda f: int(f.get("frame_index", 0)))

    # Phase 2 (A1) — pre-compute hard keyframe boundary indices.  Cheap one-pass
    # walk, used later to tag each scene group's first frame.  Always computed
    # so the data is available; consumers gate on the EYES_PHASE2_GUARDS flag.
    hard_keyframe_indices = _hard_keyframe_indices(sorted_frames, threshold)

    groups: list[list[dict]] = []
    current_group: list[dict] = []
    scene_first_hash: Optional[int] = None
    scene_first_fp: frozenset[str] = frozenset()

    for frame in sorted_frames:
        # Accept either "dhash" (explicit) or "hash" (Eyes internal field name)
        frame_hash = _parse_dhash(frame.get("dhash") or frame.get("hash"))
        frame_fp = _text_fingerprint(frame.get("extracted_text", ""))
        is_kf = bool(frame.get("is_keyframe", False))

        if not current_group:
            current_group.append(frame)
            scene_first_hash = frame_hash
            scene_first_fp = frame_fp
        elif _exceeds_threshold(
            frame_hash, scene_first_hash, threshold,
            frame_fp, scene_first_fp, is_kf,
        ):
            groups.append(current_group)
            current_group = [frame]
            scene_first_hash = frame_hash
            scene_first_fp = frame_fp
        else:
            current_group.append(frame)

    if current_group:
        groups.append(current_group)

    scenes: list[dict] = []
    for scene_index, group in enumerate(groups):
        # Representative frame: highest OCR confidence
        rep_frame = max(
            group,
            key=lambda f: float(f.get("ocr_confidence", 0.0)),
        )

        # OCR text: concatenated, sentence-deduplicated
        seen_sentences: set[str] = set()
        ocr_parts: list[str] = []
        for f in group:
            for sentence in (f.get("extracted_text", "") or "").split("."):
                cleaned = sentence.strip()
                if cleaned and cleaned not in seen_sentences:
                    seen_sentences.add(cleaned)
                    ocr_parts.append(cleaned)
        ocr_text = ". ".join(ocr_parts)

        # screen_name: priority chain
        #   1. Frame-level page_title from eyes (most reliable when present)
        #   2. LLaVA description first sentence (when not a template)
        #   3. Short OCR title extraction (up to 60 chars, not raw dump)
        #   4. URL-semantic name derived from detected_url (e.g. "USAA — Life › Quote")
        #   5. Fallback: "Scene N"
        frame_page_title = (rep_frame.get("page_title") or "").strip()
        description = rep_frame.get("description") or rep_frame.get("llava_description") or ""
        first_sentence = description.split(".")[0].strip()[:120]

        # detected_url — try standard URL first, then OCR-mangled bare domains
        # (computed before screen_name so we can use it as fallback)
        url_match = _URL_PATTERN.search(ocr_text)
        if url_match:
            detected_url = url_match.group(0)
        else:
            bare_match = _BARE_DOMAIN_PATTERN.search(ocr_text)
            if bare_match:
                host_part = bare_match.group(1).replace(" ", ".")
                tld = bare_match.group(2)
                path = bare_match.group(3) or ""
                detected_url = f"https://{host_part}.{tld}{path}"
            else:
                detected_url = ""

        # screen_name — priority chain (generic, works across all recording types)
        #
        # 1. URL-semantic name derived from detected_url: most reliable for web
        #    recordings because the URL encodes the exact page state and is not
        #    affected by browser chrome OCR noise.  Skipped for non-URL apps.
        # 2. Cleaned LLaVA/heuristic page_title: reliable for desktop apps
        #    (Excel, mainframe, Outlook) where there is no URL.  Must be
        #    stripped of browser chrome keywords first (e.g. "New Incognito Tab").
        # 3. Non-template LLaVA description first sentence.
        # 4. Short OCR title extraction (heuristic, <60 chars).
        # 5. Fallback: "Scene N".
        url_name = _screen_name_from_url(detected_url) if detected_url else ""
        cleaned_title = _clean_page_title(frame_page_title)
        if url_name:
            screen_name = url_name
        elif cleaned_title:
            screen_name = cleaned_title
        elif first_sentence and not _TEMPLATE_DESC.match(first_sentence):
            screen_name = first_sentence
        else:
            ocr_title = _extract_ocr_title(ocr_text)
            screen_name = ocr_title or f"Scene {scene_index + 1}"

        # Timestamps
        # Primary: use timestamp_ms or timestamp_seconds from the frame data.
        # Fallback: when all timestamps are zero (e.g. screenshot-only sessions
        # where no video timeline exists), synthesize relative positions from
        # frame_index so the UI shows meaningful step durations rather than
        # every scene at 0:00.  The assumed interval is configurable.
        _ASSUMED_MS_PER_FRAME = 3000  # 3 seconds between frames when no real timing

        def _ms(f: dict) -> int:
            ts_ms = f.get("timestamp_ms")
            if ts_ms is not None:
                return int(ts_ms)
            ts_sec = f.get("timestamp_seconds")
            if ts_sec is not None:
                return int(float(ts_sec) * 1000)
            return 0

        raw_timestamps = [_ms(f) for f in group]
        has_real_timing = any(t > 0 for t in raw_timestamps)

        if has_real_timing:
            start_ms = min(raw_timestamps)
            end_ms = max(raw_timestamps)
        else:
            # No real timing: use frame_index to derive relative position
            frame_indices = [int(f.get("frame_index", 0)) for f in group]
            start_ms = min(frame_indices) * _ASSUMED_MS_PER_FRAME
            end_ms = max(frame_indices) * _ASSUMED_MS_PER_FRAME

        # Completeness confidence
        has_llava = bool(description)
        completeness_confidence = 1.0 if has_llava else 0.5

        # Duration: compute explicit ms and quality flag
        duration_ms_val = end_ms - start_ms
        if not has_real_timing:
            duration_quality = "estimated"
        elif len(group) == 1:
            # Single-frame scene: start == end; UI should show "instantaneous"
            duration_quality = "estimated"
        else:
            duration_quality = "exact"

        # Scene state summary — structured, confidence-aware label object
        scene_state_summary = _build_scene_state_summary(
            screen_name=screen_name,
            detected_url=detected_url,
            ocr_text=ocr_text,
            description=description,
            app_type=rep_frame.get("application_type", "unknown"),
            has_real_timing=has_real_timing,
            group_size=len(group),
            completeness_confidence=completeness_confidence,
        )
        scene_quality = scene_state_summary["state_quality"]

        # Deterministic scene_id: same artifact + scene_index always → same UUID
        scene_id = str(uuid.uuid5(_NS, f"scene:{artifact_id}:{scene_index}"))

        # Phase 2 (A1) — anti-merge guards.  Computed unconditionally so data
        # is available to downstream consumers and the diff harness; the
        # actual *use* of these fields in `_merge_same_url_scenes` is gated
        # behind EYES_PHASE2_GUARDS.
        first_frame_idx = int(group[0].get("frame_index", -1))
        has_keyframe_boundary = first_frame_idx in hard_keyframe_indices
        # Structural fingerprint: chrome-stripped content tokens.  Strong
        # signal for distinguishing form-fill states on the same URL where
        # raw OCR Jaccard would over-state similarity.
        visible_text_fingerprint = list(_structural_fingerprint(ocr_text))

        scenes.append({
            "scene_id": scene_id,
            "artifact_id": artifact_id,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "representative_frame_id": rep_frame.get("frame_id"),
            "scene_index": scene_index,
            "screen_name": screen_name,
            "ocr_text": ocr_text,
            "detected_url": detected_url,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "app_instance_id": None,  # populated in Phase 5
            "completeness_confidence": completeness_confidence,
            # Propagate application_type from representative frame for app_segmenter
            "app_type": rep_frame.get("application_type", "unknown"),
            # Phase 8 — structured state summary and quality
            "scene_state_summary": scene_state_summary,
            "duration_ms": duration_ms_val,
            "duration_quality": duration_quality,
            "scene_quality": scene_quality,
            # Phase 2 (A1) — anti-merge guard signals.  See module docstring.
            "has_keyframe_boundary": has_keyframe_boundary,
            "visible_text_fingerprint": visible_text_fingerprint,
            # Placeholder slots back-filled by spine after Phase 6/7.
            # Empty here; the merge logic only treats them as guards when
            # populated, so an empty list/string is a no-op signal.
            "detected_controls": [],
            "entry_action": "",
            "exit_action": "",
            # Caller uses this to back-fill visual_frames.scene_id
            "frame_ids": [f.get("frame_id") for f in group if f.get("frame_id")],
        })

    # Post-processing: merge adjacent scenes on the same URL to prevent
    # over-fragmentation from minor visual changes (scroll, hover, repaint).
    scenes = _merge_same_url_scenes(scenes, artifact_id)

    # Semantic pass: collapse adjacent scenes that share the same logical
    # state_key (e.g. 9 frames of "estimate review" → 1 step).  Operates on
    # the persisted scene_state_summary so it kicks in even when raw OCR
    # similarity falls below the URL-pass threshold due to LLaVA description
    # drift or cursor-induced repaint noise.
    scenes = _merge_by_state_key(scenes, artifact_id)

    # Fill end_ms for single-frame scenes from the next scene's start_ms so
    # the timeline reflects the actual time the screen was visible. Without
    # this, every scene that contains exactly one sampled frame has
    # start_ms == end_ms and duration_ms == 0, which breaks downstream timing
    # analytics (transition_ms, action timing) even when the source timing
    # signal was real.
    _fill_single_frame_durations(scenes)

    # Intra-scene step extraction: a scene represents one URL/page; the SME
    # can perform many user actions inside it (form fills, dropdown selects,
    # button clicks).  Diff each scene's constituent frames pair-by-pair and
    # stamp the resulting actions into scene_state_summary['frame_actions']
    # so downstream consumers (test/automation generators) and the visual
    # flow page can render in-page steps without inventing them at display
    # time.
    _annotate_scenes_with_frame_actions(scenes, sorted_frames)

    return scenes


def _fill_single_frame_durations(scenes: list[dict]) -> None:
    """In-place: give single-frame scenes a real duration from the next scene.

    A scene built from one sampled frame has identical start_ms and end_ms.
    The user actually saw that screen until the next frame landed, so the
    correct end_ms is the next scene's start_ms (clamped to be strictly
    greater than the current start_ms to avoid going backwards through merges).

    When the timing source wasn't real to begin with (duration_quality already
    'estimated' because no timestamps existed), we leave it alone — there is
    no real signal to recover.

    The last scene, with no successor, keeps its zero span. Callers that care
    about that scene's end can use the canonical artifact's duration_seconds.
    """
    if len(scenes) < 2:
        return
    for i, scene in enumerate(scenes):
        if scene.get("duration_ms", 0) > 0:
            continue
        # Only extend scenes that had real timing — don't manufacture a span
        # for scenes built from frame_index estimates.
        start = scene.get("start_ms", 0)
        if start <= 0:
            continue
        if i + 1 >= len(scenes):
            continue
        next_start = scenes[i + 1].get("start_ms", 0)
        if next_start <= start:
            continue
        scene["end_ms"] = next_start
        scene["duration_ms"] = next_start - start
        scene["duration_quality"] = "inferred"


def _annotate_scenes_with_frame_actions(
    scenes: list[dict], all_frames: list[dict],
) -> None:
    """Run intra-scene step extraction and stamp results onto each scene.

    Operates in place: scene_state_summary['frame_actions'] is populated for
    every scene that has ≥2 frames.  Scenes with a single frame contribute
    no actions (there is nothing to diff).  Imports the extractor lazily so
    test environments without the optional helper module degrade gracefully.
    """
    try:
        from .step_extractor import extract_intra_scene_steps
    except Exception:  # pragma: no cover — defensive import guard
        return

    frame_lookup = {f.get("frame_id"): f for f in all_frames if f.get("frame_id")}
    for scene in scenes:
        frame_ids = scene.get("frame_ids") or []
        scene_frames = [frame_lookup[fid] for fid in frame_ids if fid in frame_lookup]
        if len(scene_frames) < 2:
            continue
        try:
            actions = extract_intra_scene_steps(scene_frames)
        except Exception:
            actions = []
        if not actions:
            continue
        summary = dict(scene.get("scene_state_summary") or {})
        summary["frame_actions"] = actions
        summary["frame_action_count"] = len(actions)
        scene["scene_state_summary"] = summary

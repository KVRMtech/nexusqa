"""Demonstrated functional E2E generator (Phase 1).

Turns the captured Pages & Forms evidence (``page_visits`` + ``page_actions``)
into ``ProductionTestCase`` objects, under one hard rule:

    Emit a step ONLY for something the user actually did and a value they
    actually entered/selected.  No alternate option values, no inferred
    behaviour, no genAI assumptions.

It is **generic across lines of business** — it reasons only about the
*structure* of the capture (URLs, actions, field labels/values), never about
any domain-specific vocabulary.

Design notes
------------
* **Page segmentation.** A recording interleaves the real navigation (visits
  that carry a URL) with OCR-only frames of the *same* page (no URL).  We
  segment the visit stream into **page groups**: each group starts at a visit
  that carries a URL and absorbs the following URL-less frames as the same
  page.  Visits before the first URL (browser chrome, "New Tab") are dropped.
* **URL is ground truth.** The page group's identity/assertion comes from the
  captured URL (path + query), which is the actual request the app made — it
  wins over OCR'd form text when they disagree.
* **Field de-duplication.** OCR relabels the same field across frames
  (``Travel Class`` / ``Class`` / ``Economy``).  Within a group we keep the
  last real value per label, then collapse labels that share a value to a
  single, cleanest label — so one logical field yields one step.
* **Placeholders excluded.** A field whose value equals its own label, or is
  an empty marker (``—``), was never filled — it is dropped, not asserted.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from nexus_sdk.models import Precondition, ProductionTestCase, ProductionTestStep

# Deterministic namespace so re-generating the same artifact yields stable
# test_ids (idempotent storage / upserts downstream).
_TEST_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

# Values that mean "the field was not actually filled".
_EMPTY_MARKERS = frozenset({"", "-", "—", "–", "n/a", "na", "none", "null", "select"})

# Booleans captured for toggles/checkboxes.
_TRUE_TOKENS = frozenset({"true", "yes", "on", "checked", "selected", "1"})
_FALSE_TOKENS = frozenset({"false", "no", "off", "unchecked", "0"})

# Action verbs that enter data into a field (carry a value).
_FILL_VERBS = frozenset({"type", "fill", "input", "enter", "select"})
# Action verbs that are pure interactions (no data).
_INTERACT_VERBS = frozenset({"click", "press", "tap", "check", "toggle"})
# Verbs we do not surface as their own step (covered by navigation assertions
# or too low-signal on their own).
_SKIP_VERBS = frozenset({"none", "navigate", "load", "wait"})

# Safety cap: a pathological recording (hundreds of page milestones) would yield
# an unbounded test with no warning. We cap the number of page groups turned into
# steps and surface a 'truncated' warning so the over-long case is VISIBLE, never
# silently emitted. Generic — a count threshold, no domain assumptions.
_MAX_GROUPS = 120


# ─── Inputs / outputs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PageVisitInput:
    """One ``page_visits`` row, reduced to what the generator needs."""

    page_visit_id: str
    sequence_index: int
    location: str
    url_host: str
    url_path: str
    url_query: str
    canonical_host: str
    source: str
    form_snapshot: Mapping[str, str]
    form_snapshot_signals: Mapping[str, object] = field(default_factory=dict)
    first_seen_ms: int = 0
    duration_ms: int = 0
    # Representative frame asset path (the page's screenshot) — per-step proof.
    frame_ref: str = ""
    # 0..1 confidence the page's URL/identity is correct. PROVEN (instrumented /
    # url_regex) ≈ 1.0; pixel-inferred (vision / title) is lower. Gates whether a
    # navigation onto this page may be asserted as fact vs. emitted UNPROVEN.
    extraction_confidence: float = 1.0


@dataclass(frozen=True)
class PageActionInput:
    """One ``page_actions`` row, reduced to what the generator needs."""

    page_visit_id: str
    subaction_index: int
    verb: str
    target_label: str
    target_kind: str
    value: str | None
    # "Where it sits" — the nearest stable landmark that locates the element
    # (captured by the anchor-extractor into page_actions.evidence_signals).
    anchor: str = ""
    anchor_kind: str = ""
    # "What happened after" — the visible outcome of the action (captured by the
    # after-extractor): drives the step's wait condition + assertion.
    after_outcome: str = ""
    after_detail: str = ""
    # Grounded navigation signal: True when the recording PROVES this action
    # advanced the page (after_outcome=='navigation' or a captured URL change).
    # Gates navigation assertions — page-group adjacency alone is NOT proof.
    navigated: bool = False
    # Menu/disclosure structure (from ``evidence_signals.qec``) — lets the generator
    # ground a MENU-GATED navigation: a dropdown item is hidden until its opener is
    # activated, so replay must click the OPENER first or the item click times out.
    # ``control_role``/``css_hint`` identify a menu item; ``expanded`` (aria-expanded)
    # identifies its disclosure toggle. All defaulted → old artifacts stay unchanged.
    control_role: str = ""
    css_hint: str = ""
    expanded: str = ""


@dataclass
class DemonstratedGenerationResult:
    """Generator output plus provenance for the API summary."""

    test_cases: list[ProductionTestCase]
    page_groups: int
    visits_total: int
    visits_used: int
    fields_demonstrated: int
    excluded_placeholder_fields: int
    # True when the recording was so large the generated test was CAPPED (only
    # the first _MAX_GROUPS page groups were turned into steps). Surfaced so a
    # 200-page recording yields a visible 'degraded' warning, never a silent
    # unbounded test. Defaulted so existing constructors/tests stay valid.
    truncated: bool = False
    truncation_reason: str = ""
    # Set when the demonstrated E2E was SUPPRESSED as an incoherent flatten — the
    # crawl reached its pages by link-following with no PROVEN click between them, so
    # a single flow would just wander across unrelated pages (login/signup included).
    # Surfaced as the honest ``no_cases_reason`` instead of shipping a misleading test.
    no_flow_reason: str = ""


@dataclass
class _PageGroup:
    """A URL milestone plus the URL-less frames that belong to the same page."""

    url_host: str
    url_path: str
    url_query: str
    canonical_host: str
    location: str
    frame_ref: str = ""
    # Provenance of this page's URL/identity (weakest across its visits): an
    # instrumented (ground_truth) or directly-OCR'd (url_regex) URL is PROVEN; a
    # vision/title-inferred URL is not, and its navigation is emitted UNPROVEN.
    source: str = ""
    extraction_confidence: float = 1.0
    visit_ids: list[str] = field(default_factory=list)
    # ordered (label, value) candidates collected across the group's frames
    field_candidates: list[tuple[str, str]] = field(default_factory=list)
    # required field labels seen on the page (even when left empty)
    required_labels: list[str] = field(default_factory=list)
    actions: list[PageActionInput] = field(default_factory=list)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    """Lowercase + collapse to alphanumerics for label/value comparison."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _strip_required(label: str) -> tuple[str, bool]:
    """Split a ``(required)`` marker off a field label."""
    m = re.search(r"\(?\brequired\b\)?", label, flags=re.IGNORECASE)
    if m:
        cleaned = (label[: m.start()] + label[m.end():]).strip(" :*-")
        return (cleaned or label.strip(), True)
    return (label.strip(), False)


def _is_boolean(value: str) -> bool:
    v = value.strip().lower()
    return v in _TRUE_TOKENS or v in _FALSE_TOKENS


def _is_true(value: str) -> bool:
    return value.strip().lower() in _TRUE_TOKENS


def _is_real_value(label: str, value: str | None) -> bool:
    """True when ``value`` is something the user actually entered/selected.

    Excludes empty markers and placeholders (value == its own field label).
    """
    if value is None:
        return False
    v = value.strip()
    if not v or v.lower() in _EMPTY_MARKERS:
        return False
    nlabel = _norm(_strip_required(label)[0])
    nvalue = _norm(v)
    if not nvalue:
        return False
    if nvalue == nlabel:
        return False  # field is showing its own placeholder label
    # Truncated-placeholder case: the value is a short prefix of the label
    # ("Flight no." under "Flight number") and carries no specifics.
    if nlabel and (nlabel.startswith(nvalue) or nvalue.startswith(nlabel)):
        if len(nvalue) < len(nlabel) and not any(c.isdigit() for c in v):
            return False
    # Abbreviation-style placeholder: the value shares its FIRST token with the
    # field label, is short, and carries no specifics — e.g. "Flight no." under
    # "Flight number".  Generic: reasons about token/shape only.
    vw = v.split()
    lw = _strip_required(label)[0].split()
    if (
        vw and lw
        and len(vw) <= 2
        and len(v) <= 14
        and not any(c.isdigit() for c in v)
        and _norm(vw[0])
        and _norm(vw[0]) == _norm(lw[0])
    ):
        return False
    return True


def _best_label(labels: Sequence[str], value: str) -> str:
    """Pick the cleanest label among several that share a value.

    Prefers a label that is NOT just the value repeated, then the most
    descriptive (most words / longest), with a stable tie-break.
    """
    nvalue = _norm(value)
    ranked = sorted(
        labels,
        key=lambda l: (
            _norm(_strip_required(l)[0]) == nvalue,  # value-as-label sorts last
            -len(_strip_required(l)[0].split()),     # more words first
            -len(_strip_required(l)[0]),              # longer first
            l,                                        # deterministic
        ),
    )
    return _strip_required(ranked[0])[0]


def _page_name(url_path: str, location: str) -> str:
    """Human page name from the last MEANINGFUL URL path segment.

    Skips short locale/section segments (``/en/us``, ``/fsr``) so the name
    reflects the actual page (``choose flights``) rather than a routing token.
    Falls back to ``home`` for locale-only roots.
    """
    segs = [s for s in (url_path or "").split("/") if s]
    for seg in reversed(segs):
        if len(seg) > 3 and not seg.isdigit():
            return seg.replace("-", " ").replace("_", " ").strip()
    # No descriptive segment. Rather than collapse every short/numeric path to
    # 'home' (which yields confusing 'home → home' case names for e.g. /42 → /99),
    # fall back to the numeric/short path tail, then to the captured location
    # label, and only call it 'home' for a truly empty (root) path.
    if segs:
        tail = segs[-1].replace("-", " ").replace("_", " ").strip()
        if tail:
            return tail
    loc = (location or "").strip()
    if loc:
        return loc
    return "home"


def _full_url(group: _PageGroup) -> str:
    host = group.url_host
    if not host:
        return group.location
    scheme = "https://"
    url = f"{scheme}{host}{group.url_path}"
    if group.url_query:
        url = f"{url}?{group.url_query}"
    return url


def _canonical_url(group: _PageGroup) -> str:
    """Stable URL for ASSERTIONS — path only, query string dropped.

    Query strings carry volatile, per-session values (ids, tokens, dates,
    cart/session keys) that differ on every run.  Asserting on them makes a
    test fail on replay and can contradict the demonstrated form values
    (e.g. a date typed in the form vs a re-encoded date in the URL).  Generic
    rule for any application: assert on the path, never the query.
    """
    host = group.url_host
    if not host:
        return group.location
    return f"https://{host}{group.url_path}"


def _locator(target_label: str, target_kind: str) -> str:
    """Neutral, resilient locator hint (label/role based, not brittle CSS).

    Exporters translate this into a framework locator (e.g. Playwright
    ``getByLabel`` / ``getByRole``).
    """
    label = (target_label or "").strip()
    kind = (target_kind or "").strip().lower()
    if not label:
        return ""
    if kind in {"button", "link", "menu"}:
        return f'role={kind}|name={label}'
    return f"label={label}"


def _upload_basename(value) -> str:
    """The recorded chosen-file NAME from an upload action's committed value.

    Browsers mask the real path as ``C:\\fakepath\\<name>`` — the basename is
    the only honest, portable part of the evidence (asserting the mask itself
    would be brittle noise, not proof).  Control characters / newlines are
    collapsed to single spaces so an odd or hostile filename can never break
    the emitted step text or the compiled spec."""
    v = " ".join(str(value or "").split()).strip().replace("\\", "/")
    return v.rsplit("/", 1)[-1].strip()


# ─── Segmentation ────────────────────────────────────────────────────────────


def _segment(
    visits: Sequence[PageVisitInput],
    actions_by_visit: Mapping[str, list[PageActionInput]],
) -> list[_PageGroup]:
    """Split the visit stream into URL-anchored page groups."""
    groups: list[_PageGroup] = []
    current: _PageGroup | None = None

    for visit in sorted(visits, key=lambda v: v.sequence_index):
        host = visit.url_host.strip()
        path = visit.url_path.strip()
        # Open a new page group on a real URL HOST (the normal path — unchanged).
        # When the host was never captured (an insecure IP-served app whose address
        # bar dropped the scheme, so OCR only recovered the PATH), fall back to
        # anchoring on a NEW non-empty PATH: the compiled test uses relative goto()
        # resolved against the Environment base-URL, so a host-less flow is still
        # runnable. The path-change guard stops repeated OCR of one page from
        # fragmenting into many groups.
        open_on_host = bool(host)
        open_on_path = (
            not host and bool(path)
            and not (current is not None and not current.url_host and current.url_path == path)
        )
        if open_on_host or open_on_path:
            current = _PageGroup(
                url_host=host,
                url_path=path,
                url_query=visit.url_query.strip(),
                canonical_host=visit.canonical_host.strip(),
                location=visit.location.strip(),
                frame_ref=visit.frame_ref or "",
                source=(visit.source or ""),
                extraction_confidence=float(getattr(visit, "extraction_confidence", 1.0) or 1.0),
            )
            groups.append(current)
        if current is None:
            # URL-less frames before the first real navigation = browser chrome.
            continue
        if not current.frame_ref and visit.frame_ref:
            current.frame_ref = visit.frame_ref
        # The group is only as trustworthy as its WEAKEST URL evidence.
        _vc = float(getattr(visit, "extraction_confidence", 1.0) or 1.0)
        if _vc < current.extraction_confidence:
            current.extraction_confidence = _vc
            current.source = visit.source or current.source
        current.visit_ids.append(visit.page_visit_id)
        for label, value in (visit.form_snapshot or {}).items():
            clean, required = _strip_required(label)
            if required and clean:
                current.required_labels.append(clean)
            if _is_real_value(label, value):
                current.field_candidates.append((label.strip(), value.strip()))
        current.actions.extend(actions_by_visit.get(visit.page_visit_id, []))

    return groups


def _resolve_fields(group: _PageGroup) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Collapse the group's field candidates to one step per logical field.

    Returns ``(text_fields, enabled_toggles, required_present)``:
      * text_fields       — [(label, value)] the user typed/selected
      * enabled_toggles   — [label] toggles the user turned ON
      * required_present  — [label] required fields that appeared (even if the
                            user did not fill them — demonstrated as *present*)
    """
    # (label → latest (seq, value)) for real, non-boolean fields; track toggles.
    last: dict[str, tuple[int, str]] = {}
    order: list[str] = []
    toggle_state: dict[str, bool] = {}
    toggle_order: list[str] = []
    for seq, (label, value) in enumerate(group.field_candidates):
        if _is_boolean(value):
            clean = _strip_required(label)[0]
            if clean not in toggle_state:
                toggle_order.append(clean)
            toggle_state[clean] = _is_true(value)  # final captured state wins
            continue
        if label not in last:
            order.append(label)
        last[label] = (seq, value)

    entries = [(label, last[label][0], last[label][1]) for label in order]

    # Cluster labels that are OCR variants of ONE logical field — keep the
    # LATEST-seen value (the user's final state), dropping abandoned
    # intermediate values. Two labels merge ONLY when one normalized clean form
    # is a WHOLE-SEGMENT prefix of the other ("date" ⊂ "date range", but NOT
    # "address" ⊂ "address line 2") AND their values are compatible (equal, or
    # one a prefix of the other). This stops two independent fields that merely
    # share a label prefix (Address / Address Line 2) from collapsing into one
    # and silently dropping a demonstrated required field.
    def _seg_prefix(a: str, b: str) -> bool:
        # Whole-segment prefix on the cleaned label words: every word of the
        # shorter label equals the corresponding word of the longer one.
        wa = _norm(a).split() if False else re.findall(r"[a-z0-9]+", (a or "").lower())
        wb = re.findall(r"[a-z0-9]+", (b or "").lower())
        if not wa or not wb:
            return False
        short, long = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
        return long[: len(short)] == short

    def _values_compatible(v1: str, v2: str) -> bool:
        a, b = _norm(v1), _norm(v2)
        if not a or not b:
            return True  # an empty/non-comparable value never blocks a merge
        return a == b or a.startswith(b) or b.startswith(a)

    used = [False] * len(entries)
    chosen: list[tuple[str, str]] = []
    for i in range(len(entries)):
        if used[i]:
            continue
        ci = _strip_required(entries[i][0])[0]
        cluster = [i]
        used[i] = True
        for j in range(i + 1, len(entries)):
            if used[j]:
                continue
            cj = _strip_required(entries[j][0])[0]
            if _seg_prefix(ci, cj) and _values_compatible(entries[i][2], entries[j][2]):
                cluster.append(j)
                used[j] = True
        best = max(cluster, key=lambda k: entries[k][1])
        chosen.append((_strip_required(entries[best][0])[0], entries[best][2]))

    # Collapse labels that share the same VALUE to one (cleanest) label — but
    # ONLY when they are the SAME logical field (same normalized clean label).
    # Two distinct labels that happen to share a value (two ZIP codes, a
    # confirm-password) are independent demonstrated fills and must each survive.
    by_value_label: dict[tuple[str, str], list[str]] = {}
    for label, value in chosen:
        key = (_norm(value), _norm(_strip_required(label)[0]))
        by_value_label.setdefault(key, []).append(label)
    text_pairs: list[tuple[str, str]] = []
    seen_value_label: set[tuple[str, str]] = set()
    for label, value in chosen:
        key = (_norm(value), _norm(_strip_required(label)[0]))
        if key in seen_value_label:
            continue
        seen_value_label.add(key)
        text_pairs.append((_best_label(by_value_label[key], value), value))

    # Emit only toggles whose FINAL captured state is ON — drops a toggle the
    # user turned on then off (e.g. Round-trip → One-way).
    toggles = [t for t in toggle_order if toggle_state.get(t)]

    seen_req: set[str] = set()
    required_present: list[str] = []
    for clean in group.required_labels:
        if _norm(clean) and _norm(clean) not in seen_req:
            seen_req.add(_norm(clean))
            required_present.append(clean)

    return text_pairs, toggles, required_present


# ─── Step construction ───────────────────────────────────────────────────────


def _observed(
    *, verb: str = "", label: str = "", kind: str = "", value: str = "",
    url: str = "", provenance: str = "demonstrated",
) -> dict:
    """The real signal captured in the recording for a step + its provenance.

    Stored as additive ``observed`` / ``provenance`` fields on the step (the
    step model allows extras).  This is the evidence column the UI surfaces and
    the raw material a script generator consumes (verb + label + role + value +
    url -> getByRole(...).fill(...)).  ``provenance`` keeps it honest:
      * ``demonstrated`` — directly seen in the video
      * ``available``    — a captured available option (not demonstrated)
      * ``inferred``     — derived (negative/boundary/un-captured transition)
    Only non-empty signals are included — never invented.
    """
    obs = {
        k: v for k, v in (
            ("verb", verb), ("label", label), ("kind", kind),
            ("value", value), ("url", url),
        ) if v
    }
    return {"observed": obs, "provenance": provenance}


# ─── Action-vs-snapshot value-conflict detection ──────────────────────────────
# A field's value can be read two ways: the KEYSTROKE value (action stream) and
# the COMMITTED value (form snapshot, which the step uses). When they genuinely
# disagree — e.g. the snapshot OCR'd a placeholder ("abc123xyz") instead of the
# typed value ("abcde323223") — we must NOT silently assert one. We flag the step
# for human confirmation (non-destructive — it still runs). Deterministic, $0.

_VALUE_ALNUM_RX = re.compile(r"[a-z0-9]+")


def _value_key(v: str) -> str:
    """Lowercase, alphanumeric-only — so '$30,000' == '30000' and pure formatting
    or punctuation differences never read as a conflict."""
    return "".join(_VALUE_ALNUM_RX.findall((v or "").lower()))


def _values_conflict(typed: str, committed: str) -> bool:
    """True when the TYPED value (action stream) genuinely disagrees with the
    COMMITTED value (form snapshot) — beyond formatting/autocomplete. Conservative:
    formatting-only diffs, prefixes/substrings (truncation), and autocomplete
    expansions (every typed token contained in the committed value) are NOT
    conflicts — only a real divergence (different digits, a placeholder) is."""
    t = _value_key(typed)
    c = _value_key(committed)
    if not t or not c or t == c:
        return False
    if t in c or c in t:  # prefix / truncation / autocomplete-extend
        return False
    toks = [tok for tok in _VALUE_ALNUM_RX.findall((typed or "").lower()) if len(tok) >= 2]
    if toks and all(tok in c for tok in toks):  # autocomplete: typed tokens ⊆ committed
        return False
    return True


def _typed_values(group: _PageGroup) -> dict[str, str]:
    """Normalized label -> the value the user TYPED (action stream), for fields the
    user actually entered. Cross-checked against the form-snapshot value the step
    uses, to surface a genuine disagreement (placeholder leak, OCR drift)."""
    out: dict[str, str] = {}
    for a in group.actions:
        verb = (a.verb or "").strip().lower()
        if verb not in ("type", "fill", "input", "select"):
            continue
        label = (a.target_label or "").strip()
        val = "" if a.value is None else str(a.value).strip()
        if not label or not val:
            continue
        out[_norm(label)] = val  # last typed wins
    return out


def _action_navigated(a: PageActionInput) -> bool:
    """True when the recording PROVES this action advanced the page — its captured
    outcome was a navigation, or a URL change was recorded. Page-group adjacency is
    NOT proof; only this grounded signal credits a click with navigating onward."""
    return bool(getattr(a, "navigated", False)) or (a.after_outcome or "").strip().lower() == "navigation"


def _path_of(url: str) -> str:
    """Path component of a full/relative URL — scheme, host, query and fragment
    dropped, trailing slash normalized. Used to compare a click's captured
    DESTINATION (``after_detail``) against the next page's URL, generically."""
    u = (url or "").strip()
    if not u:
        return ""
    u = re.sub(r"^[a-zA-Z][\w+.-]*://", "", u)      # drop scheme
    u = u.split("?", 1)[0].split("#", 1)[0]           # drop query / fragment
    slash = u.find("/")
    path = u[slash:] if slash >= 0 else "/"
    return path.rstrip("/") or "/"


# A control that is HIDDEN inside a collapsed menu/disclosure and only becomes
# clickable after its opener is activated. Two STRONG, generic signals only:
#   * ARIA ``role="menuitem"`` — only genuine popup-menu items carry it; and
#   * the Bootstrap ``dropdown-item`` class — the specific class of an item that is
#     display:none until its ``dropdown-toggle`` is opened.
# We deliberately do NOT match the bare ``menu-item`` layout class: CMS themes
# (WordPress et al.) put it on ALWAYS-VISIBLE top-level nav links, so matching it
# would prepend a spurious "open the menu" step before a directly-clickable link.
_MENU_ITEM_ROLE = "menuitem"
_MENU_ITEM_CSS_RX = re.compile(r"\bdropdown-item\b", re.IGNORECASE)


def _is_menu_item(a: PageActionInput) -> bool:
    """True when this control lives inside a collapsed menu/disclosure, so a replay
    must OPEN the menu before clicking it. Strong signals only (ARIA menuitem role /
    Bootstrap dropdown-item) — never a generic layout class a visible link also uses."""
    return (
        (getattr(a, "control_role", "") or "").strip().lower() == _MENU_ITEM_ROLE
        or bool(_MENU_ITEM_CSS_RX.search(getattr(a, "css_hint", "") or ""))
    )


def _is_disclosure_opener(a: PageActionInput) -> bool:
    """True when this click is a disclosure TOGGLE that OPENS a menu — it carries an
    ``aria-expanded`` state and did NOT itself navigate. The ARIA-standard menu-open
    signal, framework-agnostic (Bootstrap dropdown-toggle, ngb, MUI, plain ARIA)."""
    return bool((getattr(a, "expanded", "") or "").strip()) and not _action_navigated(a)


def _grounded_commit_sequence(
    clicks: list[PageActionInput], next_url: str,
) -> list[PageActionInput] | None:
    """The GROUNDED transition click(s) for a page that advances to ``next_url``.

    Prefers the click the recording PROVES reached ``next_url`` — the one whose
    captured destination (``after_detail``) matches — over the chronologically-last
    click, which is often exploratory noise (a footer credit link, an abandoned
    side-trip). This is the fix for a non-navigating click being selected as the
    transition and then hard-asserting a navigation it never caused.

    Returns ``None`` when no click grounded a navigation to ``next_url`` (the caller
    falls back to the label/position heuristic, and the boundary stays UNPROVEN —
    never a fabricated hard assertion). When the grounded click is a MENU ITEM, the
    disclosure opener that made it clickable is prepended so replay opens the menu
    first. Dialog-confirmation handling (:func:`_submit_sequence`) is preserved for
    the truncated prefix."""
    if not next_url:
        return None
    want = _path_of(next_url)
    if not want or want == "/":
        return None
    idx = None
    for i, c in enumerate(clicks):
        if _action_navigated(c) and _path_of(c.after_detail) == want:
            idx = i  # last click that PROVED arrival at next_url
    if idx is None:
        return None
    nav = clicks[idx]
    seq = _submit_sequence(clicks[: idx + 1])  # keeps any dialog-opener for the commit
    if _is_menu_item(nav):
        # Prepend the CONTIGUOUS run of disclosure openers immediately preceding the
        # item (chronological order preserved). Walking the adjacent run — not a
        # single last-wins pick — reproduces a NESTED/mega-menu's full open sequence
        # (outer toggle → inner toggle → item) and keeps the item's real opener even
        # when an unrelated disclosure was opened right before it. A non-opener click
        # breaks the run: we do NOT reach across it to grab an arbitrary far opener
        # (that would fabricate an unlinked step) — better an honest miss than a
        # mis-attributed menu-open.
        openers: list[PageActionInput] = []
        j = idx - 1
        while j >= 0 and _is_disclosure_opener(clicks[j]):
            openers.append(clicks[j])
            j -= 1
        openers = [o for o in reversed(openers) if o not in seq]
        if openers:
            seq = [*openers, *seq]
    return seq


# Sources whose URL is GROUND TRUTH (instrumented) or a directly-OCR'd address
# bar — trustworthy enough to ASSERT a navigation onto. Everything else (vision /
# title / scene-URL / missing-page) is pixel-inferred and emitted UNPROVEN.
_PROVEN_SOURCES = frozenset({"ground_truth", "url_regex"})

# Sources trustworthy enough to NAVIGATE a test's ENTRY to (weaker bar than
# _PROVEN_SOURCES, which gates asserting a URL as fact): instrumented events,
# bar-zone OCR reads, scene-detected URLs and vision address-bar reads. A
# content-mined path (url_ocr_path_only) or a screen-name/window-title guess
# must never become page.goto() step 1 of a generated test.
_ENTRY_TRUSTED_SOURCES = frozenset({
    # url_scene is EXCLUDED: scene detected_urls come from the same pixel-regex
    # sweep as content URLs and can carry a dropdown-mined path; only
    # instrumented, bar-zone-OCR and vision address-bar reads anchor an entry.
    "ground_truth", "url_regex", "llm_inferred",
})


def _entry_trusted(group: "_PageGroup") -> bool:
    """True when a page group's URL is safe to use as the flow's ENTRY
    navigation. Keys off the recorded provenance -- generic, no app logic."""
    return (
        (group.source or "").strip().lower() in _ENTRY_TRUSTED_SOURCES
        and bool(group.canonical_host or group.url_host)
    )


def _page_proven(group: _PageGroup) -> bool:
    """True when a page's URL/identity is PROVEN (instrumented or directly OCR'd at
    high confidence). A pixel-inferred page is NOT asserted as fact so test-gen
    never green-washes a guessed URL. Generic — keys off the recorded source."""
    return (group.source or "").strip().lower() in _PROVEN_SOURCES \
        and float(getattr(group, "extraction_confidence", 1.0) or 1.0) >= 0.9


def _typed_field_pairs(group: _PageGroup) -> list[tuple[str, str]]:
    """Ordered (clean_label, value) for fields the user actually TYPED (action
    stream, verb=type/fill/input). These are grounded fills that live in
    page_actions even when the form_snapshot came back empty — without them the
    typed values are dropped and the data file goes unused. Real values only,
    booleans routed out, last-typed-wins per label."""
    order: list[str] = []
    last: dict[str, str] = {}
    labels: dict[str, str] = {}
    for a in group.actions:
        if (a.verb or "").strip().lower() not in ("type", "fill", "input"):
            continue
        raw = (a.target_label or "").strip()
        val = "" if a.value is None else str(a.value).strip()
        if not raw or not val:
            continue
        clean = _strip_required(raw)[0]
        if _is_boolean(val) or not _is_real_value(clean, val):
            continue
        key = _norm(clean)
        if not key:
            continue
        if key not in last:
            order.append(key)
            labels[key] = clean
        last[key] = val  # last-typed wins
    return [(labels[k], last[k]) for k in order]


# ── Suite generation (P1 variants / P3 branches / P4 negatives / P5 grades) ──

_MAX_VARIANT_CASES = 6
_MAX_TOTAL_CASES = 12


def _demonstrated_alternates(groups: "Sequence[_PageGroup]") -> list[tuple[str, str, str]]:
    """[(label, final_value, alternate_value)] for fields where the recording
    demonstrated MORE THAN ONE value (typed then changed). Grounded by
    construction: every value was shown on video. Order-preserving, deduped."""
    out: list[tuple[str, str, str]] = []
    per_label: dict[str, list[str]] = {}
    label_display: dict[str, str] = {}
    for grp in groups:
        for act in grp.actions:
            verb = str(getattr(act, "verb", "") or "").casefold()
            if verb not in ("type", "select"):
                continue
            lbl = str(getattr(act, "target_label", "") or "").strip()
            val = str(getattr(act, "value", "") or "").strip()
            if not lbl or len(val) < 2:
                continue
            k = _norm(lbl)
            label_display.setdefault(k, lbl)
            seq = per_label.setdefault(k, [])
            if not seq or _norm(seq[-1]) != _norm(val):
                seq.append(val)
    for k, seq in per_label.items():
        distinct: list[str] = []
        for v in seq:
            if all(_norm(v) != _norm(d) for d in distinct):
                distinct.append(v)
        if len(distinct) >= 2:
            final = distinct[-1]
            for alt in distinct[:-1]:
                out.append((label_display[k], final, alt))
    return out


def _clone_step(step: "ProductionTestStep", **updates):
    try:
        return step.model_copy(update=updates)
    except AttributeError:  # pydantic v1 fallback
        return step.copy(update=updates)


def _variant_cases(
    base_steps: "list[ProductionTestStep]",
    alternates: list[tuple[str, str, str]],
    *,
    artifact_id: str,
    host: str,
) -> "list[ProductionTestCase]":
    cases = []
    for label, final, alt in alternates[:_MAX_VARIANT_CASES]:
        idx = next(
            (i for i, s in enumerate(base_steps)
             if f"' in the '{label}' field" in (s.action or "")
             and f"'{final}'" in (s.action or "")),
            None,
        )
        if idx is None:
            continue
        steps = list(base_steps)
        steps[idx] = _clone_step(
            steps[idx],
            action=f"Enter '{alt}' in the '{label}' field",
            expected=f"'{label}' shows '{alt}'",
            expected_result=f"'{label}' shows '{alt}'",
            data_ref=alt,
        )
        tid = str(uuid.uuid5(
            _TEST_ID_NAMESPACE,
            f"{artifact_id}:variant:{_norm(label)}={_norm(alt)}"))
        cases.append(ProductionTestCase(
            test_id=tid,
            name=f"Variant — {label}: '{alt}' ({host})",
            description=(
                f"Demonstrated-alternate variant: the recording shows the user "
                f"entering '{alt}' for '{label}' before settling on '{final}'. "
                f"This case replays the SAME flow with the demonstrated "
                f"alternate — every value was shown on video, nothing assumed."),
            steps=steps,
            expected_outcome=(
                f"The flow completes with '{label}' = '{alt}' — the application "
                f"accepts the demonstrated alternate value."),
            preconditions=[],
            priority="P1_high",
            type="functional",
            tags=["demonstrated", "variant", "demonstrated-alternate",
                  "pages_and_forms"],
        ))
    return cases


def _split_revisit_branch(
    groups: "Sequence[_PageGroup]",
) -> tuple["list[_PageGroup]", "list[_PageGroup]"]:
    """(trunk, branch): first clean revisit pair A..A (same canonical URL,
    >=1 page between, trunk stays >=2) — the middle is a demonstrated
    side-exploration. No revisit -> (groups, [])."""
    canon = [f"{(x.canonical_host or x.url_host or '').lower()}{x.url_path or ''}"
             for x in groups]
    for i in range(len(groups) - 2):
        if not canon[i]:
            continue
        for j in range(i + 2, len(groups)):
            if canon[j] == canon[i]:
                trunk = list(groups[:i + 1]) + list(groups[j + 1:])
                branch = list(groups[i:j + 1])
                if len(trunk) >= 2 and len(branch) >= 2:
                    return trunk, branch
    return list(groups), []


def _negative_case(
    base_steps: "list[ProductionTestStep]",
    groups: "Sequence[_PageGroup]",
    *,
    artifact_id: str,
    host: str,
) -> "ProductionTestCase | None":
    """One grounded negative: the first REQUIRED field that also has a
    demonstrated fill step. Truncate the trunk right after that step, replace
    the fill with leave-empty-and-proceed. Required-ness was DEMONSTRATED; the
    exact validation message was not -> marked UNPROVEN."""
    for grp in groups:
        try:
            _tf, _tg, required = _resolve_fields(grp)
        except Exception:
            continue
        for label in required or []:
            idx = next(
                (i for i, s in enumerate(base_steps)
                 if f"' in the '{label}' field" in (s.action or "")),
                None,
            )
            if idx is None:
                continue
            steps = [
                _clone_step(s, step_number=k + 1)
                for k, s in enumerate(base_steps[:idx + 1])
            ]
            steps[-1] = _clone_step(
                steps[-1],
                action=(f"Leave the '{label}' field EMPTY and attempt to "
                        f"proceed to the next page"),
                expected=(f"The application blocks progression and flags "
                          f"'{label}' as required. [UNPROVEN: the exact "
                          f"validation message was not demonstrated in the "
                          f"recording]"),
                expected_result=(f"Progression is blocked; '{label}' is "
                                 f"flagged as required. [UNPROVEN message]"),
                data_ref="",
            )
            tid = str(uuid.uuid5(
                _TEST_ID_NAMESPACE,
                f"{artifact_id}:negative:{_norm(label)}"))
            return ProductionTestCase(
                test_id=tid,
                name=f"Negative — required field '{label}' left empty ({host})",
                description=(
                    f"'{label}' was demonstrated as a REQUIRED field. This case "
                    f"replays the flow up to it, leaves it empty and attempts to "
                    f"proceed. The block-on-empty expectation follows from the "
                    f"demonstrated required-ness; the exact validation message "
                    f"is UNPROVEN (not demonstrated) and must not be asserted."),
                steps=steps,
                expected_outcome=(
                    f"The application refuses to proceed while '{label}' is "
                    f"empty. [UNPROVEN: exact message/behavior detail]"),
                preconditions=[],
                priority="P2_medium",
                type="negative",
                tags=["negative", "unproven-expectation", "pages_and_forms"],
            )
    return None


def _detect_data_carry(groups: "Sequence[_PageGroup]") -> list:
    """P6: values entered on an EARLIER page that re-appear among a LATER
    page's demonstrated field values (the coverage-amount-on-estimate
    pattern). Evidence-only cross-page dataflow — both sightings were
    demonstrated; nothing is fabricated."""
    carry: list = []
    seen: dict = {}
    for gi, grp in enumerate(groups):
        try:
            fields, _t, _r = _resolve_fields(grp)
        except Exception:
            continue
        page = _page_name(grp.url_path, grp.location)
        for lbl, val in fields:
            nv = _norm(str(val))
            if len(nv) < 3:
                continue
            if nv in seen and seen[nv][0] < gi:
                if not any(x.get("value_norm") == nv for x in carry):
                    carry.append({"value": str(val), "value_norm": nv,
                                  "entered_on": seen[nv][1], "reappears_on": page})
            else:
                seen.setdefault(nv, (gi, page))
    for x in carry:
        x.pop("value_norm", None)
    return carry


# A flow with NO grounded navigation backbone AND at least this many UNGROUNDED
# page-jumps is the crawler's BFS traversal flattened — not a coherent demonstrated
# journey. It would just wander across unrelated pages (into login/signup), so it is
# suppressed rather than shipped as a misleading "E2E".
_MIN_FLATTEN_JUMPS = 3


def _navigation_backbone(steps: "Sequence[ProductionTestStep]") -> tuple[int, int]:
    """``(grounded, inferred)`` count of page-to-page transitions in the flow: a
    boundary asserted as a REAL navigation (a demonstrated ``Verify navigated`` step)
    vs an honest UNPROVEN jump (an inferred one). Distinguishes a coherent demonstrated
    journey (a grounded backbone) from a link-following exploration flattened."""
    grounded = inferred = 0
    for s in steps:
        if not (getattr(s, "action", "") or "").startswith("Verify the application navigated"):
            continue
        obs = getattr(s, "observed", None) or {}
        prov = str(getattr(s, "provenance", "") or obs.get("provenance") or "").casefold()
        if prov == "demonstrated":
            grounded += 1
        elif prov == "inferred":
            inferred += 1
    return grounded, inferred


def _grade_case(case: "ProductionTestCase") -> None:
    """P5: per-case evidence grade from the case's OWN steps. A = every step
    demonstrated; B = <=2 inferred/unproven steps; C = more. Appended as tags
    so every consumer (UI, export, client) sees WHY to trust the case."""
    inferred = 0
    for s in case.steps:
        prov = str(getattr(s, "provenance", "") or
                   getattr(s, "observed_provenance", "") or "").casefold()
        text = f"{s.expected or ''} {s.action or ''}"
        if prov == "inferred" or "UNPROVEN" in text:
            inferred += 1
    grade = "A" if inferred == 0 else ("B" if inferred <= 2 else "C")
    case.tags = list(case.tags or []) + [
        f"evidence-grade:{grade}", f"inferred-steps:{inferred}",
        f"total-steps:{len(case.steps)}"]


def _build_steps(groups: Sequence[_PageGroup]) -> tuple[list[ProductionTestStep], int, bool]:
    """Build ordered, logically-reconstructed test steps from page groups.

    Returns ``(steps, fields_used, truncated)``. ``truncated`` is True when the
    recording exceeded ``_MAX_GROUPS`` page milestones and only the first
    ``_MAX_GROUPS`` were turned into steps — so an over-long test is surfaced as
    degraded rather than emitted unbounded and silent.
    """
    steps: list[ProductionTestStep] = []
    n = 0
    fields_used = 0
    prev_navigated = True  # the entry page has no prior boundary to prove

    # Bound the work: keep the entry page + first _MAX_GROUPS-1 milestones so the
    # flow's start (and a coherent prefix) is preserved; the tail is dropped and
    # flagged. Conservative — normal recordings (< _MAX_GROUPS groups) are unchanged.
    truncated = len(groups) > _MAX_GROUPS
    if truncated:
        groups = groups[:_MAX_GROUPS]

    # FLOW-LEVEL fill dedup: when the vision pass splits one wizard page into
    # multiple visits, the SAME persisted field state is re-observed and would
    # be emitted as a repeated step (Texas entered twice). A (field, value)
    # pair is entered ONCE per flow — first observation wins; a later visit
    # re-showing it is state persistence, not a second user entry.
    seen_fills: set = set()

    for gi, group in enumerate(groups):
        url = _full_url(group)
        canon = _canonical_url(group)
        page_name = _page_name(group.url_path, group.location)
        next_canon = _canonical_url(groups[gi + 1]) if gi + 1 < len(groups) else ""
        next_path = groups[gi + 1].url_path if gi + 1 < len(groups) else ""
        group_start = len(steps)  # tag this group's steps with its screenshot

        # 1) Navigation onto the page.  The entry step navigates to the full URL;
        #    every later assertion checks the PATH only (see _canonical_url).
        n += 1
        if gi == 0:
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Open {url}",
                expected=f"The {page_name} page is displayed",
                expected_result=f"The {page_name} page is displayed",
                selector=f"url={url}",
                **_observed(verb="navigate", url=url),
            ))
        else:
            # The boundary INTO this page is asserted as a real navigation ONLY when
            # the PREVIOUS group grounded-navigated here; otherwise the transition was
            # never captured (e.g. the inventory→cart→checkout gap) — demote to
            # 'inferred' so the compiler emits an honest UNPROVEN comment, never a
            # toHaveURL that can only ever fail RED.
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Verify the application navigated to {canon}",
                expected=f"URL path is {group.url_path or canon} and the {page_name} page is displayed",
                expected_result=f"URL path is {group.url_path or canon} and the {page_name} page is displayed",
                selector=f"url={canon}",
                **_observed(verb="navigate", url=group.url_path or canon,
                            provenance=("demonstrated" if (prev_navigated and _page_proven(group)) else "inferred")),
            ))

        text_fields, toggles, required_present = _resolve_fields(group)
        typed_by_label = _typed_values(group)  # keystroke values, to cross-check
        # Fold in grounded fills that live ONLY in the action stream (verb=type) —
        # the form_snapshot extraction can come back empty even when the user clearly
        # typed values (those land in page_actions, not the snapshot). form_snapshot
        # stays AUTHORITATIVE: a field already resolved from the snapshot is not
        # overwritten, so the existing value_conflict human-confirm path still fires.
        _snapshot_labels = {_norm(lbl) for lbl, _ in text_fields}
        for _tlbl, _tval in _typed_field_pairs(group):
            if _norm(_tlbl) in _snapshot_labels:
                continue
            text_fields.append((_tlbl, _tval))
            _snapshot_labels.add(_norm(_tlbl))

        # 2) Fill demonstrated text/select fields.
        for label, value in text_fields:
            _fill_sig = (_norm(label), _norm(str(value)))
            if _fill_sig in seen_fills:
                continue  # persisted state re-observed on a split visit
            seen_fills.add(_fill_sig)
            fields_used += 1
            n += 1
            step = ProductionTestStep(
                step_number=n,
                action=f"Enter '{value}' in the '{label}' field",
                expected=f"'{label}' shows '{value}'",
                expected_result=f"'{label}' shows '{value}'",
                selector=_locator(label, "field"),
                data_ref=value,
                **_observed(verb="type", label=label, kind="field", value=value),
            )
            # Surface a genuine action-vs-snapshot disagreement (e.g. the snapshot
            # OCR'd a placeholder) so a human confirms the value — never assert one
            # reading as certain. Non-destructive: the step still runs.
            typed = typed_by_label.get(_norm(label))
            if typed is not None and _values_conflict(typed, value):
                step.observed["value_conflict"] = {"typed": typed, "committed": value}
            steps.append(step)

        # 2b) Attach demonstrated FILE UPLOADS (verb=upload): the crawler chose a
        #     seed document on this file input and the browser COMMITTED the
        #     filename (after.outcome=value_committed) — grounded evidence, so it
        #     becomes a real step. The compiler's upload rung recreates the seed
        #     and re-attaches it via setInputFiles (never a fill on a file input).
        for a in group.actions:
            if (a.verb or "").strip().lower() != "upload":
                continue
            _ulabel = (a.target_label or "").strip()
            _uname = _upload_basename(a.value)
            if not _ulabel or not _uname:
                continue  # a nameless/valueless upload cannot be re-grounded — skip honestly
            _usig = ("upload", _norm(_ulabel), _norm(_uname))
            if _usig in seen_fills:
                continue  # persisted state re-observed on a split visit
            seen_fills.add(_usig)
            fields_used += 1
            n += 1
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Attach '{_uname}' to the '{_ulabel}' field",
                expected=f"'{_ulabel}' holds the chosen file '{_uname}'",
                expected_result=f"'{_ulabel}' holds the chosen file '{_uname}'",
                selector=_locator(_ulabel, "field"),
                data_ref=_uname,
                **_observed(verb="upload", label=_ulabel, kind="file", value=_uname),
            ))

        # 3) Toggles the user turned on.
        for label in toggles:
            n += 1
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Select '{label}'",
                expected=f"'{label}' is selected",
                expected_result=f"'{label}' is selected",
                selector=_locator(label, "field"),
                **_observed(verb="select", label=label, kind="toggle"),
            ))

        # 4) Pure interactions the user performed (clicks/hovers/etc.), in order.
        next_group_proven = _page_proven(groups[gi + 1]) if gi + 1 < len(groups) else False
        interaction_steps = _interaction_steps(group, next_canon, next_group_proven)
        for st in interaction_steps:
            n += 1
            st.step_number = n
            steps.append(st)

        # 4b) The page advances but NO action was captured to cause it: insert an
        #     explicit transition step instead of silently jumping to the next
        #     page.  Honest + generic — every app has un-captured transitions.
        if next_path and not interaction_steps:
            n += 1
            next_name = _page_name(groups[gi + 1].url_path, groups[gi + 1].location)
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Proceed to the {next_name} page",
                expected=f"The {next_name} page opens",
                expected_result=f"The {next_name} page opens",
                **_observed(verb="navigate", provenance="inferred"),
            ))

        # 5) Required-but-unfilled fields: assert presence (demonstrated as shown),
        #    never assert entry (the user did not fill them).
        if required_present:
            n += 1
            joined = ", ".join(required_present)
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Verify the form requires: {joined}",
                expected=f"Required fields are present: {joined}",
                expected_result=f"Required fields are present: {joined}",
                **_observed(verb="assert_required", label=joined, kind="form"),
            ))

        # Tag every step on this page with the page's screenshot (proof per step).
        if group.frame_ref:
            for st in steps[group_start:]:
                st.screenshot = group.frame_ref

        # Gate the NEXT group's "verify navigated here" boundary on whether THIS
        # group's EMITTED transition step actually reached that page — a PER-ACTION
        # signal, not group-level. The old ``any(action navigated)`` green-washed the
        # boundary whenever *some* click in the group navigated (even elsewhere), so a
        # non-navigating transition (a footer credit link) still hard-asserted the
        # next URL and failed RED. Now only a transition step carrying a grounded
        # ``next_url`` for the next page proves the boundary; anything else demotes it
        # to an honest UNPROVEN comment.
        _final_int = interaction_steps[-1] if interaction_steps else None
        prev_navigated = bool(
            _final_int and next_path
            and _path_of((_final_int.observed or {}).get("next_url", ""))
            == _path_of(next_canon or next_path)
        )

    return steps, fields_used, truncated


# Signals that a click landed inside a confirmation dialog / modal / overlay.
_DIALOG_RX = re.compile(
    r"\b(dialog|modal|confirm(?:ation)?|pop[\s-]?up|overlay|are you sure)\b",
    re.IGNORECASE,
)

# GENERIC forward-commit verbs — a control whose label commits the flow onward.
# Used ONLY as a fallback to credit a navigation when the recording captured NO
# outcome signal at all (un-enriched). Strictly generic semantics, NEVER app or
# page names — keeps the gate working on ANY UI (zero saucedemo/app strings).
_COMMIT_RX = re.compile(
    r"\b(continue|next|submit|checkout|proceed|place\s*order|pay|confirm|finish|"
    r"complete|review\s*order|sign[\s-]?in|log[\s-]?in)\b",
    re.IGNORECASE,
)


def _submit_sequence(clicks: list) -> list:
    """The trailing click(s) that COMMIT the page. Normally just the final click;
    but when the final click is a CONFIRMATION inside a dialog/modal, also include
    the click that OPENED it (both were recorded) so replay doesn't try to confirm a
    dialog that was never opened. Grounded + conservative: fires ONLY when the final
    click signals a dialog AND the preceding click plausibly opened it — it visibly
    opened a dialog, or the dialog just repeats the same action label
    ('Submit claim' → 'Submit claim (confirmation dialog)')."""
    if not clicks:
        return []
    last = clicks[-1]
    last_sig = " ".join([
        last.target_label or "", last.after_detail or "", last.after_outcome or "",
    ])
    if len(clicks) >= 2 and _DIALOG_RX.search(last_sig):
        opener = clicks[-2]
        opener_after = " ".join([opener.after_detail or "", opener.after_outcome or ""])
        last_core = _norm(re.sub(r"\(.*?\)", "", last.target_label or ""))
        opener_core = _norm(opener.target_label or "")
        opened_a_dialog = bool(_DIALOG_RX.search(opener_after))
        same_action = bool(
            opener_core and last_core
            and (opener_core == last_core or last_core.startswith(opener_core)
                 or opener_core in last_core)
        )
        if opened_a_dialog or same_action:
            return [opener, last]
    return [last]


def _interaction_steps(group: _PageGroup, next_url: str, next_group_proven: bool = False) -> list[ProductionTestStep]:
    """The SUBMIT action(s) for the page — normally the final click/press, plus the
    modal-OPEN click when the final click confirms a dialog (see _submit_sequence).

    Hovers, scrolls, and earlier exploratory clicks (menu tabs, abandoned
    side-trips) are dropped: only the trailing commit click(s) before the page
    advances are part of the demonstrated forward flow.  ``group.actions`` is
    already in chronological (visit → subaction) order.
    """
    clicks = [
        a for a in group.actions
        if (a.verb or "").strip().lower() in {"click", "press", "tap"}
        and (a.target_label or "").strip()
    ]
    if not clicks:
        return []
    # Prefer the GROUNDED transition (the click that PROVED arrival at next_url,
    # plus its menu opener) over the chronologically-last click. Falls back to the
    # label/position heuristic when nothing grounded a navigation to next_url.
    seq = _grounded_commit_sequence(clicks, next_url) or _submit_sequence(clicks)
    out: list[ProductionTestStep] = []
    for i, a in enumerate(seq):
        is_final = i == len(seq) - 1
        label = a.target_label.strip()
        after_outcome = (a.after_outcome or "").strip().lower()
        # GROUNDED navigation gate. Only the FINAL commit click can advance the page,
        # and even then we credit it with reaching the next group's URL ONLY when the
        # recording PROVES it: the action's own outcome was a navigation (or a URL
        # change was captured), OR — for un-enriched recordings with NO captured
        # outcome at all — the click bears a GENERIC forward-commit label. A recorded
        # 'stayed' outcome is authoritative and always wins (no commit-label override).
        # Page-group adjacency alone is NOT proof — that is what wrongly pinned
        # /checkout-step-one onto the non-navigating 'Add to cart' click.
        navigated = _action_navigated(a)
        # Destination-aware credit: pin next_url onto this click ONLY when its OWN
        # captured destination matches next_url — or none was captured (un-enriched,
        # fall back to the raw navigated flag). Closes a mis-attribution where the
        # trailing click navigated ELSEWHERE (a footer/side-trip link) yet still had
        # the next page's URL pinned onto it and hard-asserted a navigation it never
        # caused. When the destinations disagree, this click did not reach next_url →
        # no assertion (the boundary stays honestly UNPROVEN), never a fabricated RED.
        nav_reaches_next = navigated and (
            not (a.after_detail or "").strip()
            or _path_of(a.after_detail) == _path_of(next_url)
        )
        commit_fallback = (not after_outcome) and bool(_COMMIT_RX.search(label))
        step_next = next_url if (is_final and next_url and (nav_reaches_next or commit_fallback)) else ""
        anchor = (a.anchor or "").strip()
        after = (a.after_detail or "").strip()
        # Fold the anchor into the step so a repeated control is unambiguous:
        # "Click 'Select' in the '10:30 AM' row".
        where = f" in the '{anchor}' {a.anchor_kind or 'section'}" if anchor else ""
        # The Expected Result reflects the OBSERVED outcome (wait + assertion):
        # navigation, a results panel appearing, a validation error, etc.
        if step_next:
            expected = f"The application proceeds to {step_next}"
            if after:
                expected = f"{expected}; {after}"
        elif after:
            expected = after
        elif not is_final and _is_disclosure_opener(a):
            # a menu OPENER preceding the grounded item click — replay opens the
            # disclosure so the hidden item becomes clickable (not a dialog confirm).
            expected = f"the '{label}'{where} menu opens, revealing its items"
        elif not is_final:
            expected = f"a confirmation step opens after '{label}'{where}"
        else:
            expected = f"'{label}'{where} is activated"
        obs = _observed(verb=(a.verb or "click").strip().lower(), label=label, kind=a.target_kind or "button")
        if anchor:
            obs["observed"]["anchor"] = anchor
            # Carry the anchor's container kind so the compiler scopes the locator to
            # the right ARIA role (row / listitem / card / region…), not just a table
            # row — disambiguates repeated controls in card/list/grid layouts.
            if (a.anchor_kind or "").strip():
                obs["observed"]["anchor_kind"] = a.anchor_kind.strip()
        if a.after_outcome:
            obs["observed"]["after"] = after or a.after_outcome
        # Carry the RECORDED next-page URL so the compiler can assert the SUBMIT
        # actually navigated there (a click step has no URL of its own). Grounded:
        # set ONLY when the gate above proved this click caused the navigation.
        # navigation_grounded (which lets the confidence scorer keep this as a hard
        # toHaveURL assertion) is set ONLY when the DESTINATION page is itself PROVEN
        # — mirroring the verify-step gate (_page_proven). When the next page is
        # pixel-inferred, we still hand the URL to the compiler as a hint but WITHOUT
        # navigation_grounded, so confidence demotes the step to REVIEW instead of
        # hard-asserting toHaveURL onto an unproven page (never green-wash).
        if step_next:
            obs["observed"]["next_url"] = step_next
            if next_group_proven:
                obs["observed"]["navigation_grounded"] = True
        out.append(ProductionTestStep(
            step_number=0,
            action=f"Click '{label}'{where}",
            expected=expected,
            expected_result=expected,
            selector=_locator(label, a.target_kind),
            **obs,
        ))
    return out


# ─── Public entry point ──────────────────────────────────────────────────────


# Unambiguous credential-entry signals (email alone is too weak — many forms have
# it; require a password/PIN/OTP/sign-in/username signal).
_CREDENTIAL_RX = re.compile(
    r"\b(password|passcode|pin|otp|one[\s-]?time|sign[\s-]?in|log[\s-]?in|username|user[\s-]?name)\b",
    re.IGNORECASE,
)


def _login_observed_before_app(visits: Iterable[PageVisitInput]) -> bool:
    """True when the recording shows credential entry (a login) BEFORE the first
    URL-keyed app page — meaning the URL-anchored test starts already authenticated
    and does NOT replay the login (the login screens are screen-name-keyed, not
    URL-keyed, so they're dropped from the URL-anchored flow). Grounded: scans only
    what was observed; never assumes auth where none was seen."""
    ordered = sorted(visits, key=lambda v: getattr(v, "sequence_index", 0) or 0)
    for v in ordered:
        if (getattr(v, "url_host", "") or "").strip():
            break  # first URL-keyed page → the app flow has begun
        labels = " ".join((getattr(v, "form_snapshot", None) or {}).keys())
        loc = getattr(v, "location", "") or ""
        if _CREDENTIAL_RX.search(labels) or _CREDENTIAL_RX.search(loc):
            return True
    return False


def _visit_password_field_count(v: PageVisitInput) -> int:
    """Count OBSERVED password fields on a visit's form
    (``form_snapshot_signals[label].type == 'password'``). The COUNT is the
    structural login distinguisher: a genuine sign-in has EXACTLY ONE password
    field, while signup / set-new-password / change-password all carry TWO or more
    (password + confirm), and a non-credential masked field (SSN/PIN/CVV/OTP) is a
    lone type='password' that fails the sign-in-commit test below."""
    signals = getattr(v, "form_snapshot_signals", None)
    if not isinstance(signals, Mapping):  # a malformed non-dict signals blob → not login
        return 0
    return sum(
        1 for sig in signals.values()
        if isinstance(sig, Mapping) and str(sig.get("type") or "").strip().lower() == "password"
    )


# A SIGN-IN commit — the label that distinguishes a login from every other
# password-bearing form. `type='password'` alone is only a MASKED-FIELD signal:
# signup (Password + Confirm), change/reset-password, and non-credential masked
# inputs (SSN, PIN, CVV, OTP, security answer) all carry type='password' but are
# NOT sign-in. The commit label is what separates them.
_SIGNIN_COMMIT_RX = re.compile(r"\b(sign[\s-]?in|log[\s-]?in|log[\s-]?on|login)\b", re.IGNORECASE)
#: Commit labels that are password-bearing but NOT a sign-in — a login is refused
#: (not dropped) if its commit matches any of these, so a create-account /
#: change-password / reset / wizard-continue page is never misclassified as login.
_NOT_SIGNIN_RX = re.compile(
    r"\b(sign[\s-]?up|signup|create|regist|enrol|reset|forgot|change|update|save|"
    r"continue|next|proceed|submit|apply|verify|confirm)\b",
    re.IGNORECASE,
)


def _is_signin_commit(action: PageActionInput) -> bool:
    """A click/submit whose label reads as SIGN-IN (and not create-account /
    change-password / reset / wizard-next). The load-bearing login distinguisher."""
    if (getattr(action, "verb", "") or "").strip().lower() not in ("click", "submit"):
        return False
    label = getattr(action, "target_label", "") or ""
    return bool(_SIGNIN_COMMIT_RX.search(label)) and not _NOT_SIGNIN_RX.search(label)


def _make_auth_precondition() -> Precondition:
    """A FRESH auth precondition (never a shared instance across cases). Says the
    URL-anchored test does NOT replay the observed login and needs a captured
    session, so a cold run doesn't fail silently at step 1."""
    return Precondition(
        description=(
            "An authenticated session is required. The recording showed a login "
            "(credential entry) before the app flow, but this URL-anchored test does "
            "NOT replay the login. Apply an Authentication profile (a captured "
            "logged-in session) so the run starts authenticated — otherwise a cold "
            "run will land on the login screen and fail at step 1."
        ),
        setup_action="Apply an authentication profile (captured logged-in session) before running.",
    )


def _group_is_login(group: "_PageGroup", visits_by_id: Mapping[str, PageVisitInput]) -> bool:
    """A LOGIN page: EXACTLY ONE password field AND a sign-in commit action.

    Both are required, and the count matters. A password field alone is a masked-field
    signal, not a login. Requiring a SINGLE password field structurally excludes the
    two-password forms a sign-in label can still appear on — signup, set-new-password,
    reset-confirmation, change-password (all password + confirm) — and a lone masked
    field (SSN/PIN/CVV/OTP) fails the sign-in-commit test. So the only failure mode is
    a REFUSAL to drop (an unrecognised login replays and fails honestly RED), never a
    wrong drop (green-wash)."""
    pw_count = sum(
        _visit_password_field_count(visits_by_id[vid])
        for vid in group.visit_ids
        if vid in visits_by_id
    )
    if pw_count != 1:  # 0 = not password-auth; 2+ = signup/set/change-password, never a sign-in
        return False
    return any(_is_signin_commit(a) for a in group.actions)


def generate_demonstrated_test_cases(
    *,
    artifact_id: str,
    page_visits: Iterable[PageVisitInput],
    page_actions: Iterable[PageActionInput],
) -> DemonstratedGenerationResult:
    """Generate the demonstrated functional E2E test case(s) for one artifact.

    Returns a single primary functional E2E that replays the demonstrated
    navigation + data, plus provenance counts.  Returns zero test cases when
    the capture has no URL-anchored flow to ground a test in.
    """
    visits = list(page_visits)
    actions = list(page_actions)

    actions_by_visit: dict[str, list[PageActionInput]] = {}
    for act in actions:
        actions_by_visit.setdefault(act.page_visit_id, []).append(act)
    for lst in actions_by_visit.values():
        lst.sort(key=lambda a: a.subaction_index)

    groups = _segment(visits, actions_by_visit)

    # A trustworthy functional E2E needs at least one navigation that lands on a
    # distinct page (entry → outcome).  Fewer than two URL milestones means the
    # recording never demonstrated a completed flow.
    if len(groups) < 2:
        return DemonstratedGenerationResult(
            test_cases=[],
            page_groups=len(groups),
            visits_total=len(visits),
            visits_used=sum(len(g.visit_ids) for g in groups),
            fields_demonstrated=0,
            excluded_placeholder_fields=_count_placeholders(visits),
        )

    # PHASE-A TRUSTED ENTRY: drop leading groups whose page identity is
    # content-mined or screen-name-inferred (browser chrome, search dropdowns).
    # The demonstrated APP flow starts at the first entry-trusted page; a
    # content-mined path must never become the script's opening goto. Only
    # applied when a trusted entry exists and >= 2 milestones survive -- the
    # honest fallback keeps the original journey rather than fabricating one.
    _first_trusted_idx = next(
        (i for i, _g in enumerate(groups) if _entry_trusted(_g)), None,
    )
    if _first_trusted_idx and len(groups) - _first_trusted_idx >= 2:
        groups = groups[_first_trusted_idx:]

    # DROP a leading LOGIN page from the URL-anchored flow. The recording shows
    # credential entry (a form with an OBSERVED password field), but the password is
    # never persisted (secret hygiene), so replaying it fails on the login screen.
    # Exactly like the old screen-name-keyed login path (which has no url_host and is
    # already excluded), a URL-keyed login is peeled off so the flow STARTS at the
    # first post-login page and the run supplies auth via a captured session (see the
    # precondition below). PRECISE: keyed on the password input TYPE, so a non-login
    # form (search-by-email, newsletter, profile-edit) is NEVER dropped. Applied only
    # when >= 2 milestones survive — a valid E2E — mirroring the trusted-entry rule.
    _visits_by_id = {v.page_visit_id: v for v in visits}
    _login_lead = 0
    while _login_lead < len(groups) and _group_is_login(groups[_login_lead], _visits_by_id):
        _login_lead += 1
    _login_group_dropped = bool(_login_lead) and (len(groups) - _login_lead >= 2)
    if _login_group_dropped:
        groups = groups[_login_lead:]

    # P3: peel one demonstrated side-exploration (revisit A..A) off the trunk
    # so the E2E stays linear and the branch becomes its own case.
    groups, _branch_groups = _split_revisit_branch(groups)

    steps, fields_used, truncated = _build_steps(groups)

    # COHERENCE GATE: a single "E2E" whose transitions are DOMINATED by ungrounded
    # page-jumps (more inferred than grounded, with >= _MIN_FLATTEN_JUMPS of them) is
    # the crawler's link-following traversal flattened — it wanders across unrelated
    # pages (home → cart → login → api list → …), routes through and fills a
    # login/signup form it happened to land on, and confuses more than it helps. A
    # coherent flow (a video journey, or a grounded click-path) has a backbone of
    # PROVEN transitions (grounded >= inferred); those are kept. The flatten is
    # suppressed in favour of the grounded per-navigation journeys (emitted
    # separately), and the reason is surfaced honestly — never a silent empty, and a
    # short flow with a single gap (inferred < _MIN_FLATTEN_JUMPS) is never swept up.
    _grounded_navs, _inferred_navs = _navigation_backbone(steps)
    if _inferred_navs >= _MIN_FLATTEN_JUMPS and _grounded_navs < _inferred_navs:
        return DemonstratedGenerationResult(
            test_cases=[],
            page_groups=len(groups),
            visits_total=len(visits),
            visits_used=sum(len(g.visit_ids) for g in groups),
            fields_demonstrated=0,
            excluded_placeholder_fields=_count_placeholders(visits),
            no_flow_reason=(
                f"No coherent functional E2E — the crawl stitched {_inferred_navs + 1} "
                "pages together but PROVED only "
                f"{_grounded_navs} of the {_grounded_navs + _inferred_navs} navigations "
                "between them, so a single flow would wander across unrelated pages "
                "(routing through login/signup). The grounded per-navigation JOURNEYS "
                "are the coherent, runnable tests; a re-crawl or richer interaction "
                "grounds more of the site."),
        )

    truncation_reason = (
        f"Large recording: {len(groups)} page milestones detected — the generated "
        f"test was capped at the first {_MAX_GROUPS} pages. Review/split the "
        f"recording into smaller flows for full coverage."
        if truncated else ""
    )

    entry = _page_name(groups[0].url_path, groups[0].location)
    outcome = _page_name(groups[-1].url_path, groups[-1].location)
    # Host-less captures (IP app, scheme dropped by OCR) leave url_host empty; the
    # flow still runs against the Environment base-URL, so fall back to a neutral
    # label rather than an empty "()".
    host = groups[0].canonical_host or groups[0].url_host or "the recorded app"
    name = f"Functional E2E: {entry} → {outcome} ({host})"
    description = (
        f"Replays the demonstrated flow on {host}: starting at '{entry}', "
        f"entering the values the user provided, and verifying the application "
        f"reaches '{outcome}'. Every step is grounded in the recording "
        f"(Pages & Forms) — no assumed data."
    )

    signature = "|".join(f"{g.url_host}{g.url_path}" for g in groups)
    test_id = str(uuid.uuid5(_TEST_ID_NAMESPACE, f"{artifact_id}:demonstrated:{signature}"))

    # Case-level Expected Result — grounded in the LAST recorded page (the flow's
    # outcome). Observable + verifiable, never invented.
    outcome_path = groups[-1].url_path or _canonical_url(groups[-1])
    expected_outcome = (
        f"The flow completes successfully — the application reaches the '{outcome}' page"
        + (f" ({outcome_path})" if outcome_path else "") + "."
    )

    preconditions = [Precondition(
        description="A supported web browser is open and the target site is reachable.",
        setup_action=f"Open {_full_url(groups[0])}",
    )]
    # Honest auth precondition: if a login was observed BEFORE the app flow, this
    # URL-anchored test does NOT replay it — say so, so a cold run doesn't fail
    # silently at step 1. Grounded (we observed credential entry); never fabricated.
    _needs_auth = _login_group_dropped or _login_observed_before_app(visits)
    if _needs_auth:
        preconditions.insert(0, _make_auth_precondition())

    test_case = ProductionTestCase(
        test_id=test_id,
        name=name,
        description=description,
        steps=steps,
        expected_outcome=expected_outcome,
        preconditions=preconditions,
        priority="P0_critical",
        type="functional",
        tags=["demonstrated", "functional", "e2e", "pages_and_forms"],
    )

    # P6: evidence-only cross-page data-carry (recorded on case + manifest).
    _carry = _detect_data_carry(groups)
    if _carry:
        test_case.description = (test_case.description or "") + (
            " Data-carry observed: " + "; ".join(
                f"'{x['value']}' entered on '{x['entered_on']}' re-appears on "
                f"'{x['reappears_on']}'" for x in _carry[:4]) + ".")
        try:
            test_case.data_carry = _carry
        except (AttributeError, ValueError, TypeError):
            pass  # model may forbid extra attrs — description still carries it

    cases: list[ProductionTestCase] = [test_case]

    # P1: demonstrated-alternate variants (user showed >1 value for a field).
    cases.extend(_variant_cases(
        steps, _demonstrated_alternates(groups),
        artifact_id=artifact_id, host=host,
    ))

    # P3: branch case for the demonstrated side-exploration, if one was peeled.
    if _branch_groups:
        try:
            b_steps, _bf, _bt = _build_steps(_branch_groups)
            b_entry = _page_name(_branch_groups[0].url_path, _branch_groups[0].location)
            cases.append(ProductionTestCase(
                test_id=str(uuid.uuid5(
                    _TEST_ID_NAMESPACE,
                    f"{artifact_id}:branch:" + "|".join(
                        f"{x.url_host}{x.url_path}" for x in _branch_groups))),
                name=f"Branch — exploration from '{b_entry}' and back ({host})",
                description=(
                    "The recording left this page, explored, and returned — a "
                    "demonstrated side-path emitted as its own case so the main "
                    "E2E stays linear. Every step was shown on video."),
                steps=b_steps,
                expected_outcome=(
                    f"The side-exploration completes and returns to '{b_entry}'."),
                preconditions=[],
                priority="P1_high",
                type="functional",
                tags=["demonstrated", "branch", "pages_and_forms"],
            ))
        except Exception:
            pass  # a branch that cannot build steps is dropped, never fabricated

    # P4: one grounded negative from demonstrated required-ness.
    _neg = _negative_case(steps, groups, artifact_id=artifact_id, host=host)
    if _neg is not None:
        cases.append(_neg)

    # Every case from a login-behind artifact starts post-login and needs a captured
    # session — attach the auth precondition to the SECONDARY cases too (variant /
    # branch / negative), not just the primary, so none fails silently at step 1
    # (the primary already carries it). Only prepend when absent.
    if _needs_auth:
        for _c in cases[1:]:
            _pcs = list(getattr(_c, "preconditions", None) or [])
            if not any("authentication profile" in (getattr(p, "setup_action", "") or "").lower()
                       for p in _pcs):
                _c.preconditions = [_make_auth_precondition(), *_pcs]

    # P5: every case self-reports its evidence grade.
    cases = cases[:_MAX_TOTAL_CASES]
    for _c in cases:
        _grade_case(_c)

    return DemonstratedGenerationResult(
        test_cases=cases,
        page_groups=len(groups),
        visits_total=len(visits),
        visits_used=sum(len(g.visit_ids) for g in groups),
        fields_demonstrated=fields_used,
        excluded_placeholder_fields=_count_placeholders(visits),
        truncated=truncated,
        truncation_reason=truncation_reason,
    )


def _count_placeholders(visits: Sequence[PageVisitInput]) -> int:
    count = 0
    for v in visits:
        for label, value in (v.form_snapshot or {}).items():
            if value is not None and value.strip() and not _is_real_value(label, value):
                count += 1
    return count


def _host_of(url: str) -> str:
    """Host component of a URL (scheme + path + query dropped, lowercased)."""
    u = re.sub(r"^[a-zA-Z][\w+.-]*://", "", (url or "").strip())
    return u.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip().lower()


# Bound on grounded-journey cases per artifact — a large crawl can capture many
# grounded navigations; keep the emitted suite sane (dedup already collapses repeats).
_MAX_JOURNEYS = 60


def generate_grounded_journeys(
    *,
    artifact_id: str,
    page_visits: Sequence[PageVisitInput],
    page_actions: Sequence[PageActionInput],
) -> list[ProductionTestCase]:
    """Emit a SHORT, fully-grounded journey for each captured navigation CLICK:
    ``open the source page → [open the menu] → click the control → verify the
    destination``.

    Unlike the demonstrated E2E — which flattens the whole BFS visit stream into one
    flow and can therefore only use a grounded click that happens to sit at a forward
    transition boundary — each journey is built DIRECTLY from one grounded click-path.
    So a menu navigation the crawler grounded on a *revisit* (e.g. Categories ▸ Hand
    Tools recorded when it returned to the home page) still becomes a runnable,
    coherent flow. Reuses the transition + disclosure-opener grounding (:func:`
    _is_menu_item` / :func:`_is_disclosure_opener`).

    PROVEN-ONLY and never green-washed: a journey is emitted ONLY for a click the
    recording proves navigated (``after_outcome=navigation`` / captured URL change)
    to a SAME-APP destination, and ONLY from a source page whose URL is trustworthy
    enough to ``goto`` (entry-trusted). Everything else is skipped, never fabricated.
    """
    by_visit: dict[str, list[PageActionInput]] = {}
    for a in page_actions:
        by_visit.setdefault(a.page_visit_id, []).append(a)
    for lst in by_visit.values():
        lst.sort(key=lambda a: a.subaction_index)

    cases: list[ProductionTestCase] = []
    seen: set = set()  # dedup by (source_path, dest_path, nav-label)

    for v in sorted(page_visits, key=lambda v: getattr(v, "sequence_index", 0) or 0):
        # Only start a journey from a page we can trustworthily open (its URL is
        # instrumented / directly OCR'd, and a host/path exists) — never from a
        # vision-guessed address.
        #
        # ``url_host`` (the REAL page hostname) is preferred over ``canonical_host``:
        # canonical_host is a registrable-domain REDUCTION (grouping key), so on a
        # multi-label host (``vkpowerlife.35-186-147-245.sslip.io`` → ``sslip.io``)
        # it is neither a goto-able address for the opening step NOR comparable to
        # the full destination hostname below — preferring it mis-killed every
        # same-app navigation as "cross-host" (live: 11-page crawl → 0 journeys).
        # On an IP or single-label host the two are identical, so nothing changes.
        src_host = (v.url_host or v.canonical_host or "").strip()
        if (v.source or "").strip().lower() not in _ENTRY_TRUSTED_SOURCES or not src_host:
            continue
        src_url = f"https://{src_host}{v.url_path}"
        src_path = _path_of(src_url) or "/"
        src_name = "home" if src_path == "/" else _page_name(v.url_path, v.location)

        clicks = [
            a for a in by_visit.get(v.page_visit_id, [])
            if (a.verb or "").strip().lower() in {"click", "press", "tap"}
            and (a.target_label or "").strip()
        ]
        for i, a in enumerate(clicks):
            if not _action_navigated(a):
                continue
            dest = (a.after_detail or "").strip()
            if not dest:
                continue  # navigated but destination not captured — cannot verify, skip
            dest_path = _path_of(dest)
            if not dest_path or dest_path == "/" or dest_path == src_path:
                continue  # no path, or a self/home nav — not a distinct journey
            if _host_of(dest) and _host_of(dest) != src_host.lower():
                continue  # left the app (external link) — not an in-app journey
            # Dedup by (destination, control): a nav-bar control that appears on every
            # page yields ONE journey (from the earliest source page — visits are in
            # sequence order), not a near-duplicate per page it was captured on.
            key = (dest_path, _norm(a.target_label))
            if key in seen:
                continue
            seen.add(key)

            # The disclosure opener(s) that made a MENU item clickable (contiguous
            # aria-expanded run immediately preceding it) — so replay opens the menu.
            openers: list[PageActionInput] = []
            if _is_menu_item(a):
                j = i - 1
                while j >= 0 and _is_disclosure_opener(clicks[j]):
                    openers.append(clicks[j])
                    j -= 1
                openers = list(reversed(openers))

            dest_canon = f"https://{src_host}{dest_path}"
            dest_name = _page_name(dest_path, "")
            steps: list[ProductionTestStep] = []
            n = 0

            n += 1
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Open {src_url}",
                expected=f"The {src_name} page is displayed",
                expected_result=f"The {src_name} page is displayed",
                selector=f"url={src_url}",
                **_observed(verb="navigate", url=src_url),
            ))
            for op in openers:
                n += 1
                steps.append(ProductionTestStep(
                    step_number=n,
                    action=f"Click '{op.target_label}'",
                    expected=f"the '{op.target_label}' menu opens, revealing its items",
                    expected_result=f"the '{op.target_label}' menu opens, revealing its items",
                    selector=_locator(op.target_label, op.target_kind),
                    **_observed(verb="click", label=op.target_label,
                                kind=op.target_kind or "button"),
                ))
            n += 1
            nav_obs = _observed(verb="click", label=a.target_label,
                                kind=a.target_kind or "link")
            # Grounded transition: the recording PROVED this click reached dest, so the
            # compiler emits a HARD toHaveURL (navigation_grounded) — the real oracle.
            nav_obs["observed"]["next_url"] = dest_canon
            nav_obs["observed"]["navigation_grounded"] = True
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Click '{a.target_label}'",
                expected=f"The application proceeds to {dest_path}",
                expected_result=f"The application proceeds to {dest_path}",
                selector=_locator(a.target_label, a.target_kind),
                **nav_obs,
            ))
            n += 1
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Verify the application navigated to {dest_canon}",
                expected=f"URL path is {dest_path} and the {dest_name} page is displayed",
                expected_result=f"URL path is {dest_path} and the {dest_name} page is displayed",
                selector=f"url={dest_canon}",
                **_observed(verb="navigate", url=dest_path, provenance="demonstrated"),
            ))
            if v.frame_ref:
                for st in steps:
                    st.screenshot = v.frame_ref  # all steps share the source page's frame

            test_id = str(uuid.uuid5(
                _TEST_ID_NAMESPACE,
                f"{artifact_id}:journey:{src_path}>{dest_path}:{_norm(a.target_label)}"))
            via = " via the menu" if openers else ""
            case = ProductionTestCase(
                test_id=test_id,
                name=f"Navigate to {dest_name} via '{a.target_label}' — from {src_name} ({src_host})",
                description=(
                    f"A grounded click-path on {src_host}: from '{src_name}', "
                    f"clicking '{a.target_label}'{via} navigates to '{dest_name}' "
                    f"({dest_path}). Every step was captured — the navigation is a "
                    f"PROVEN URL oracle, not an assumed transition."),
                steps=steps,
                expected_outcome=(
                    f"Clicking '{a.target_label}' reaches the '{dest_name}' page "
                    f"({dest_path})."),
                preconditions=[Precondition(
                    description="A supported web browser is open and the target site is reachable.",
                    setup_action=f"Open {src_url}",
                )],
                priority="P1_high",
                type="functional",
                tags=["demonstrated", "grounded-journey", "navigation", "pages_and_forms"],
            )
            _grade_case(case)
            cases.append(case)
            if len(cases) >= _MAX_JOURNEYS:
                return cases
    return cases


def generate_form_flow_journeys(
    *,
    artifact_id: str,
    page_visits: Sequence[PageVisitInput],
    page_actions: Sequence[PageActionInput],
) -> list[ProductionTestCase]:
    """Emit ONE fully-grounded FORM FLOW per demonstrated Phase-B submit:
    ``open the form page → replay each demonstrated fill (its committed value)
    → click the operator-APPROVED submit → verify the demonstrated destination``.

    The grounded-journey generator deliberately skips ``submit``-verb actions
    (they are gated interactions, not free navigation), so a crawl that filled a
    form AND crossed the submit boundary (fences.submit_approvals) produced the
    complete evidence for the app's CORE business flow — a quote, a transfer, a
    checkout — but no case consumed it (live: the VKPower quote submit landed on
    ``/quote?submitted=1&…`` with the premium displayed, and generate still
    returned only nav journeys). This emitter closes that gap.

    PROVEN-ONLY, nothing invented:
      * only fills whose committed value was read back from the live control;
      * only a submit the recording PROVES navigated (after_outcome=navigation)
        to a captured same-app destination;
      * the verify step asserts the FULL demonstrated destination URL
        (path+query — the query derives from the replayed fills, so an
        identical replay reproduces it);
      * source page must be entry-trusted (instrumented URL), same rule as
        grounded journeys.

    Recording shape (live-verified): the explorer records the submit ACTION on
    the DESTINATION visit (the post-submit state), while the fills live on the
    FORM visit that precedes it. The pairing is therefore: for each proven
    submit, the source form is the latest visit at-or-before it (by
    sequence_index) that HAS demonstrated fills and shares the destination's
    path (a form that posts to itself), falling back to the immediate
    predecessor visit with fills (a form that posts to a different path). Both
    the same-visit and split-visit shapes are covered by tests.
    """
    by_visit: dict[str, list[PageActionInput]] = {}
    for a in page_actions:
        by_visit.setdefault(a.page_visit_id, []).append(a)
    for lst in by_visit.values():
        lst.sort(key=lambda a: a.subaction_index)

    def _visit_fills(visit_id: str) -> list[PageActionInput]:
        """Demonstrated fills on one visit: committed, non-empty values only; a
        control filled twice keeps its LAST committed value (the state the
        submit was made in)."""
        fills_by_label: dict[str, PageActionInput] = {}
        for a in by_visit.get(visit_id, []):
            if (a.verb or "").strip().lower() in ("type", "select") \
                    and (a.target_label or "").strip() and (a.value or "").strip():
                fills_by_label[_norm(a.target_label)] = a
        return sorted(fills_by_label.values(), key=lambda a: a.subaction_index)

    ordered = sorted(page_visits, key=lambda v: getattr(v, "sequence_index", 0) or 0)
    cases: list[ProductionTestCase] = []
    seen: set = set()  # dedup by (source_path, submit-label)

    for vi, sv in enumerate(ordered):
        sv_host = (sv.url_host or sv.canonical_host or "").strip()
        if (sv.source or "").strip().lower() not in _ENTRY_TRUSTED_SOURCES or not sv_host:
            continue
        submits = [
            a for a in by_visit.get(sv.page_visit_id, [])
            if (a.verb or "").strip().lower() == "submit"
            and (a.target_label or "").strip()
            and (bool(getattr(a, "navigated", False))
                 or (a.after_outcome or "").strip().lower() == "navigation")
        ]
        if not submits:
            continue

        for sub in submits:
            dest = (sub.after_detail or "").strip()
            if not dest:
                continue  # navigated but destination not captured — cannot verify
            dest_host = _host_of(dest)
            if dest_host and dest_host != sv_host.lower():
                continue  # left the app — not this app's business flow
            dest_path = _path_of(dest) or "/"

            # ── Pair the submit with its SOURCE form visit ────────────────
            # Prefer the latest at-or-before visit (entry-trusted, same host)
            # with demonstrated fills on the DESTINATION's path (a form posting
            # to itself); else the immediate predecessor visit with fills.
            src_visit = None
            for cand in reversed(ordered[: vi + 1]):
                if (cand.source or "").strip().lower() not in _ENTRY_TRUSTED_SOURCES:
                    continue
                cand_host = (cand.url_host or cand.canonical_host or "").strip()
                if not cand_host or cand_host.lower() != sv_host.lower():
                    continue
                if (cand.url_path or "/") == dest_path and _visit_fills(cand.page_visit_id):
                    src_visit = cand
                    break
            if src_visit is None and vi > 0:
                prev = ordered[vi - 1]
                if _visit_fills(prev.page_visit_id):
                    src_visit = prev
            if src_visit is None:
                continue  # no demonstrated fills to ground the flow — skip, never invent

            fills = _visit_fills(src_visit.page_visit_id)
            src_host = (src_visit.url_host or src_visit.canonical_host or "").strip()
            src_query = (getattr(src_visit, "url_query", "") or "").strip()
            src_url = (f"https://{src_host}{src_visit.url_path}"
                       + (f"?{src_query}" if src_query else ""))
            src_path = _path_of(src_url) or "/"
            src_name = ("home" if src_path == "/"
                        else _page_name(src_visit.url_path, src_visit.location))
            # Same path WITH a different query is a legitimate submit landing
            # (?submitted=1&…); only a byte-identical URL proves nothing moved.
            if dest.rstrip("/") == src_url.rstrip("/"):
                continue
            key = (src_path, _norm(sub.target_label))
            if key in seen:
                continue
            seen.add(key)

            steps: list[ProductionTestStep] = []
            n = 1
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Open {src_url}",
                expected=f"The {src_name} page is displayed with its form",
                expected_result=f"The {src_name} page is displayed with its form",
                selector=f"url={src_url}",
                **_observed(verb="navigate", url=src_url),
            ))
            for f in fills:
                n += 1
                verb = (f.verb or "").strip().lower()
                phrase = "Select" if verb == "select" else "Enter"
                steps.append(ProductionTestStep(
                    step_number=n,
                    action=f"{phrase} '{f.value}' in '{f.target_label}'",
                    expected=f"'{f.target_label}' holds '{f.value}'",
                    expected_result=f"'{f.target_label}' holds '{f.value}'",
                    selector=_locator(f.target_label, f.target_kind),
                    **_observed(verb=verb, label=f.target_label,
                                kind=f.target_kind or "text_field",
                                value=str(f.value)),
                ))
            n += 1
            sub_obs = _observed(verb="click", label=sub.target_label,
                                kind=sub.target_kind or "button")
            # The recording PROVED this submit reached dest — hard URL oracle.
            sub_obs["observed"]["next_url"] = dest
            sub_obs["observed"]["navigation_grounded"] = True
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Click '{sub.target_label}'",
                expected=f"The form submits and the application proceeds to {dest_path}",
                expected_result=f"The form submits and the application proceeds to {dest_path}",
                selector=_locator(sub.target_label, sub.target_kind),
                **sub_obs,
            ))
            n += 1
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Verify the application navigated to {dest}",
                expected=f"URL is {dest} and the submitted {src_name} result is displayed",
                expected_result=f"URL is {dest} and the submitted {src_name} result is displayed",
                selector=f"url={dest}",
                **_observed(verb="navigate", url=dest, provenance="demonstrated"),
            ))
            if src_visit.frame_ref:
                for st in steps:
                    st.screenshot = src_visit.frame_ref

            test_id = str(uuid.uuid5(
                _TEST_ID_NAMESPACE,
                f"{artifact_id}:formflow:{src_path}:{_norm(sub.target_label)}"))
            case = ProductionTestCase(
                test_id=test_id,
                name=(f"{src_name.title()} flow: fill the form and submit via "
                      f"'{sub.target_label}' ({src_host})"),
                description=(
                    f"The demonstrated {src_name} business flow on {src_host}: "
                    f"{len(fills)} field(s) filled with their captured values, then "
                    f"'{sub.target_label}' (operator-approved Phase-B submit) — the "
                    f"recording PROVED the submit navigated to {dest_path}. Every "
                    f"step replays captured evidence; the destination URL is a hard "
                    f"oracle, never an assumed transition."),
                steps=steps,
                expected_outcome=(
                    f"Submitting the {src_name} form via '{sub.target_label}' "
                    f"reaches {dest_path} with the submitted result displayed."),
                preconditions=[Precondition(
                    description="A supported web browser is open and the target site is reachable.",
                    setup_action=f"Open {src_url}",
                )],
                priority="P0_critical",
                type="functional",
                tags=["demonstrated", "grounded-form-flow", "submit", "pages_and_forms"],
            )
            _grade_case(case)
            cases.append(case)
            if len(cases) >= _MAX_JOURNEYS:
                return cases
    return cases

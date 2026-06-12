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


@dataclass
class DemonstratedGenerationResult:
    """Generator output plus provenance for the API summary."""

    test_cases: list[ProductionTestCase]
    page_groups: int
    visits_total: int
    visits_used: int
    fields_demonstrated: int
    excluded_placeholder_fields: int


@dataclass
class _PageGroup:
    """A URL milestone plus the URL-less frames that belong to the same page."""

    url_host: str
    url_path: str
    url_query: str
    canonical_host: str
    location: str
    frame_ref: str = ""
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
            )
            groups.append(current)
        if current is None:
            # URL-less frames before the first real navigation = browser chrome.
            continue
        if not current.frame_ref and visit.frame_ref:
            current.frame_ref = visit.frame_ref
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

    # Cluster labels whose normalized clean form prefixes another
    # ("date" ⊂ "dates") — one logical field — and keep the LATEST-seen value
    # (the user's final state), dropping abandoned intermediate values.
    used = [False] * len(entries)
    chosen: list[tuple[str, str]] = []
    for i in range(len(entries)):
        if used[i]:
            continue
        ni = _norm(_strip_required(entries[i][0])[0])
        cluster = [i]
        used[i] = True
        for j in range(i + 1, len(entries)):
            if used[j]:
                continue
            nj = _norm(_strip_required(entries[j][0])[0])
            if ni and nj and (ni.startswith(nj) or nj.startswith(ni)):
                cluster.append(j)
                used[j] = True
        best = max(cluster, key=lambda k: entries[k][1])
        chosen.append((_strip_required(entries[best][0])[0], entries[best][2]))

    # Collapse labels that share the same VALUE to one (cleanest) label.
    by_value: dict[str, list[str]] = {}
    for label, value in chosen:
        by_value.setdefault(_norm(value), []).append(label)
    text_pairs: list[tuple[str, str]] = []
    seen_value: set[str] = set()
    for label, value in chosen:
        nv = _norm(value)
        if nv in seen_value:
            continue
        seen_value.add(nv)
        text_pairs.append((_best_label(by_value[nv], value), value))

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


def _build_steps(groups: Sequence[_PageGroup]) -> tuple[list[ProductionTestStep], int]:
    """Build ordered, logically-reconstructed test steps from page groups."""
    steps: list[ProductionTestStep] = []
    n = 0
    fields_used = 0

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
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Verify the application navigated to {canon}",
                expected=f"URL path is {group.url_path or canon} and the {page_name} page is displayed",
                expected_result=f"URL path is {group.url_path or canon} and the {page_name} page is displayed",
                selector=f"url={canon}",
                **_observed(verb="navigate", url=group.url_path or canon),
            ))

        text_fields, toggles, required_present = _resolve_fields(group)
        typed_by_label = _typed_values(group)  # keystroke values, to cross-check

        # 2) Fill demonstrated text/select fields.
        for label, value in text_fields:
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
        interaction_steps = _interaction_steps(group, next_canon)
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

    return steps, fields_used


# Signals that a click landed inside a confirmation dialog / modal / overlay.
_DIALOG_RX = re.compile(
    r"\b(dialog|modal|confirm(?:ation)?|pop[\s-]?up|overlay|are you sure)\b",
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


def _interaction_steps(group: _PageGroup, next_url: str) -> list[ProductionTestStep]:
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
    seq = _submit_sequence(clicks)
    out: list[ProductionTestStep] = []
    for i, a in enumerate(seq):
        is_final = i == len(seq) - 1
        # Only the FINAL click in the commit sequence advances the page; an earlier
        # click (e.g. the one that opens a confirmation dialog) does not navigate.
        step_next = next_url if is_final else ""
        label = a.target_label.strip()
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
        # this is the observed next page, not an inferred target. Only on the FINAL
        # click — the modal-open click doesn't navigate.
        if step_next:
            obs["observed"]["next_url"] = step_next
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

    steps, fields_used = _build_steps(groups)

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
    if _login_observed_before_app(visits):
        preconditions.insert(0, Precondition(
            description=(
                "An authenticated session is required. The recording showed a login "
                "(credential entry) before the app flow, but this URL-anchored test does "
                "NOT replay the login. Apply an Authentication profile (a captured "
                "logged-in session) so the run starts authenticated — otherwise a cold "
                "run will land on the login screen and fail at step 1."
            ),
            setup_action="Apply an authentication profile (captured logged-in session) before running.",
        ))

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

    return DemonstratedGenerationResult(
        test_cases=[test_case],
        page_groups=len(groups),
        visits_total=len(visits),
        visits_used=sum(len(g.visit_ids) for g in groups),
        fields_demonstrated=fields_used,
        excluded_placeholder_fields=_count_placeholders(visits),
    )


def _count_placeholders(visits: Sequence[PageVisitInput]) -> int:
    count = 0
    for v in visits:
        for label, value in (v.form_snapshot or {}).items():
            if value is not None and value.strip() and not _is_real_value(label, value):
                count += 1
    return count

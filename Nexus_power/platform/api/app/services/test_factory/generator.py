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
    first_seen_ms: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class PageActionInput:
    """One ``page_actions`` row, reduced to what the generator needs."""

    page_visit_id: str
    subaction_index: int
    verb: str
    target_label: str
    target_kind: str
    value: str | None


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
    visit_ids: list[str] = field(default_factory=list)
    # ordered (label, value) candidates collected across the group's frames
    field_candidates: list[tuple[str, str]] = field(default_factory=list)
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
    """Human page name from the last meaningful URL path segment."""
    segs = [s for s in (url_path or "").split("/") if s]
    if segs:
        return segs[-1].replace("-", " ").replace("_", " ").strip()
    loc = (location or "").strip()
    return loc[:60] if loc else "page"


def _full_url(group: _PageGroup) -> str:
    host = group.url_host
    if not host:
        return group.location
    scheme = "https://"
    url = f"{scheme}{host}{group.url_path}"
    if group.url_query:
        url = f"{url}?{group.url_query}"
    return url


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
        has_url = bool(visit.url_host.strip())
        if has_url:
            current = _PageGroup(
                url_host=visit.url_host.strip(),
                url_path=visit.url_path.strip(),
                url_query=visit.url_query.strip(),
                canonical_host=visit.canonical_host.strip(),
                location=visit.location.strip(),
            )
            groups.append(current)
        if current is None:
            # URL-less frames before the first real navigation = browser chrome.
            continue
        current.visit_ids.append(visit.page_visit_id)
        for label, value in (visit.form_snapshot or {}).items():
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
    # Last real value per exact label (later frames win — final state).
    last_value: dict[str, str] = {}
    label_order: list[str] = []
    required_present: list[str] = []
    seen_required: set[str] = set()

    # Required-but-empty fields appear in the snapshot as label "(required)"
    # with an empty value; capture them from the RAW group snapshots.
    for label, value in group.field_candidates:
        if label not in last_value:
            label_order.append(label)
        last_value[label] = value

    text_pairs: list[tuple[str, str]] = []
    enabled_toggles: list[str] = []
    # Collapse labels that share a value.
    by_value: dict[str, list[str]] = {}
    for label in label_order:
        value = last_value[label]
        if _is_boolean(value):
            if _is_true(value):
                enabled_toggles.append(_strip_required(label)[0])
            continue
        by_value.setdefault(_norm(value), []).append(label)

    emitted_value: set[str] = set()
    for label in label_order:
        value = last_value[label]
        if _is_boolean(value):
            continue
        nvalue = _norm(value)
        if nvalue in emitted_value:
            continue
        emitted_value.add(nvalue)
        text_pairs.append((_best_label(by_value[nvalue], value), value))

    # De-dup toggles while preserving order.
    seen_tog: set[str] = set()
    toggles = [t for t in enabled_toggles if not (t in seen_tog or seen_tog.add(t))]

    # Required-present: scan raw labels (incl. empty-valued) for "(required)".
    for label, _value in group.field_candidates:
        clean, req = _strip_required(label)
        if req and _norm(clean) not in seen_required:
            seen_required.add(_norm(clean))
            required_present.append(clean)

    return text_pairs, toggles, required_present


# ─── Step construction ───────────────────────────────────────────────────────


def _build_steps(groups: Sequence[_PageGroup]) -> tuple[list[ProductionTestStep], int]:
    """Build ordered, logically-reconstructed test steps from page groups."""
    steps: list[ProductionTestStep] = []
    n = 0
    fields_used = 0

    for gi, group in enumerate(groups):
        url = _full_url(group)
        page_name = _page_name(group.url_path, group.location)
        next_url = _full_url(groups[gi + 1]) if gi + 1 < len(groups) else ""
        next_path = groups[gi + 1].url_path if gi + 1 < len(groups) else ""

        # 1) Navigation onto the page.
        n += 1
        if gi == 0:
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Open {url}",
                expected=f"The {page_name} page is displayed",
                expected_result=f"The {page_name} page is displayed",
                selector=f"url={url}",
            ))
        else:
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Verify the application navigated to {url}",
                expected=f"URL is {group.url_path or url} and the {page_name} page is displayed",
                expected_result=f"URL is {group.url_path or url} and the {page_name} page is displayed",
                selector=f"url={url}",
            ))

        text_fields, toggles, required_present = _resolve_fields(group)

        # 2) Fill demonstrated text/select fields.
        for label, value in text_fields:
            fields_used += 1
            n += 1
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Enter '{value}' in the '{label}' field",
                expected=f"'{label}' shows '{value}'",
                expected_result=f"'{label}' shows '{value}'",
                selector=_locator(label, "field"),
                data_ref=value,
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
            ))

        # 4) Pure interactions the user performed (clicks/hovers/etc.), in order.
        interaction_steps = _interaction_steps(group, next_url)
        for st in interaction_steps:
            n += 1
            st.step_number = n
            steps.append(st)

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
            ))

    return steps, fields_used


def _interaction_steps(group: _PageGroup, next_url: str) -> list[ProductionTestStep]:
    """Click/hover/etc. steps from the group's actions (deduped, ordered)."""
    out: list[ProductionTestStep] = []
    seen: set[tuple[str, str]] = set()
    ordered = sorted(group.actions, key=lambda a: a.subaction_index)
    for a in ordered:
        verb = (a.verb or "").strip().lower()
        label = (a.target_label or "").strip()
        if verb in _SKIP_VERBS or verb in _FILL_VERBS:
            continue  # fills already represented from the form snapshot
        if verb not in _INTERACT_VERBS and verb not in {"hover", "scroll"}:
            continue
        if not label:
            continue
        key = (verb, _norm(label))
        if key in seen:
            continue
        seen.add(key)
        if verb in {"click", "press", "tap"}:
            action = f"Click '{label}'"
            expected = (
                f"The application proceeds to {next_url}" if next_url
                else f"'{label}' is activated"
            )
        elif verb in {"check", "toggle"}:
            action = f"Toggle '{label}'"
            expected = f"'{label}' state changes"
        elif verb == "hover":
            action = f"Hover over '{label}'"
            expected = f"'{label}' menu/options are revealed"
        else:  # scroll
            action = f"Scroll to '{label}'"
            expected = f"'{label}' is visible"
        out.append(ProductionTestStep(
            step_number=0,
            action=action,
            expected=expected,
            expected_result=expected,
            selector=_locator(label, a.target_kind),
        ))
    return out


# ─── Public entry point ──────────────────────────────────────────────────────


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
    host = groups[0].canonical_host or groups[0].url_host
    name = f"Functional E2E: {entry} → {outcome} ({host})"
    description = (
        f"Replays the demonstrated flow on {host}: starting at '{entry}', "
        f"entering the values the user provided, and verifying the application "
        f"reaches '{outcome}'. Every step is grounded in the recording "
        f"(Pages & Forms) — no assumed data."
    )

    signature = "|".join(f"{g.url_host}{g.url_path}" for g in groups)
    test_id = str(uuid.uuid5(_TEST_ID_NAMESPACE, f"{artifact_id}:demonstrated:{signature}"))

    test_case = ProductionTestCase(
        test_id=test_id,
        name=name,
        description=description,
        steps=steps,
        preconditions=[Precondition(
            description="A supported web browser is open and the target site is reachable.",
            setup_action=f"Open {_full_url(groups[0])}",
        )],
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

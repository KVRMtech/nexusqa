"""Control INVENTORY — refine raw browser controls into the compiler's vocabulary.

The injected walker (:mod:`app.inventory_js`) returns raw, DOM-shaped control
dicts.  :func:`build_inventory` turns each into a :class:`ControlRecord` whose
TOP-LEVEL field names are exactly what the deterministic Playwright compiler
binds on, plus a ``qec`` diagnostics bucket for the signals that have NO
compiler rung (``role`` / ``testid`` / ``css_hint`` / ``input_type``).

Verified compiler contract (``platform/api/app/services/script_factory/
compiler.py`` + ``platform/api/app/services/test_factory/{service,generator}.py``,
read 2026-07-08):

  * The compiler binds by the USER-FACING accessible NAME only — ``_ladder``
    emits getByLabel/getByRole(name=…)/getByText rungs (compiler.py:297-331).
    ``testid``/``css_hint`` have NO rung → diagnostics only.
  * ``observed.kind`` == ``page_actions.target_kind`` (generator.py:1100); it
    decides link-vs-button on a click, and feeds ``_refine_kind`` together with
    ``form_snapshot_signals[label].type`` — the dominant form-field signal
    (compiler.py:174-208; build_field_meta compiler.py:153-171).
  * ``anchor`` travels as ``evidence_signals.anchor = {label, kind}``
    (service.py:154-165 → generator.py:1101-1107); ``kind`` MUST be a key the
    compiler's ``_ANCHOR_ROLE`` recognises (compiler.py:216-225) or the scope
    silently defaults to ``row``.  Its ABSENCE is legitimate evidence — an
    anchor is attached ONLY when a control's ``(frame, role, name)`` collides.
  * ``page_actions.target_kind`` and ``form_snapshot_signals.type`` use two
    DIFFERENT small vocabularies — :func:`target_kind_for` /
    :func:`form_signal_for` map a :class:`ControlRecord` onto each so the
    downstream writer (``platform/qe-central/app/substrate/writer.py``) stays a
    dumb mapper.

Danger classification delegates to the fail-closed guard — the single source
of truth for the refuse policy (``app.guard.classify_action_verb(button_name,
url, refuse_pack) -> VerbClassification``, guard.py:432-465).  An actuator
(button / link / menu item / tab) whose accessible name matches an irreversible
verb is flagged a never-click leaf; with no vetted ``RefusePack`` the guard —
and this module's ImportError-only local fallback — treat every actuator as
irreversible (fail-closed, nothing proven safe).  Non-actuators (text fields,
etc.) are never flagged.  This module stays unit-testable without a browser;
the guard ships in the same package, so the local fallback is defensive only.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable, Optional, TypedDict

logger = logging.getLogger(__name__)

# ─── Vocabularies (pinned to the verified compiler contract) ──────────────────

#: The refined ``kind`` vocabulary — mirrors the compiler's ``observed.kind``
#: space (compiler.py:174-208, :306-327).
CONTROL_KINDS = frozenset(
    {"text", "date", "select", "link", "toggle", "button", "checkbox", "radio"}
)

#: page_actions.target_kind vocabulary (substrate schema TARGET_KINDS —
#: alembic 035_page_actions.py:64-65 / qe-central schema.py).
TARGET_KINDS = frozenset(
    {"button", "link", "dropdown", "text_field", "checkbox", "radio",
     "menu_item", "tab", "other"}
)

#: kind → page_actions.target_kind.
_TARGET_KIND_BY_KIND = {
    "link": "link", "button": "button", "select": "dropdown",
    "checkbox": "checkbox", "radio": "radio", "toggle": "checkbox",
    "text": "text_field", "date": "text_field",
}

#: kind → form_snapshot_signals[label].type (the ``control`` value the compiler's
#: ``_refine_kind`` reads, compiler.py:181-208).  Non-value controls (button /
#: link) are NOT form fields and get no signal.
_FORM_SIGNAL_TYPE_BY_KIND = {
    "select": "select", "checkbox": "checkbox", "radio": "radio",
    "toggle": "toggle", "date": "date", "text": "text",
}

#: HTML constraint attributes carried on a form signal — the rule the
#: APPLICATION declared about its own field, never a value. Mirrors
#: ``_VALIDATION_KEYS`` in qe-central ``app/services/catalog.py``, which reads
#: exactly these keys off ``form_snapshot_signals[label]`` to build a catalogue
#: question's ``validation``. The services share no library: change both or
#: neither, or the catalogue silently loses the rule again.
_VALIDATION_KEYS = ("pattern", "minlength", "maxlength", "min", "max", "step")

#: Landmark ARIA role → the ``anchor.kind`` the compiler's ``_ANCHOR_ROLE``
#: understands (compiler.py:216-225).  A landmark not in this map degrades to
#: the text-based ``block`` scope (compiler.py:264-278), which the compiler
#: resolves against any container that holds both the anchor text and the
#: control — the honest generic fallback.
_ANCHOR_KIND_BY_LANDMARK = {
    "row": "row", "listitem": "listitem", "article": "article",
    "region": "region", "group": "group", "gridcell": "gridcell",
    "cell": "cell", "list": "list", "listbox": "listbox", "option": "option",
    "menuitem": "menuitem", "tabpanel": "tabpanel", "dialog": "dialog",
}

#: Roles that make a control INTERACTIVE (kept in the inventory; also the
#: fingerprint's interactive set — see :mod:`app.fingerprint`).
INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox",
    "checkbox", "radio", "switch", "menuitem", "menuitemcheckbox",
    "menuitemradio", "tab", "option", "slider", "spinbutton", "gridcell",
})

#: Date-ish committed value — mirrors the frozen compiler ``_DATE_RX``
#: (compiler.py:29-33) so a text input whose value looks like a date refines to
#: ``date`` identically to the way the compiler would treat it.
_DATE_RX = re.compile(
    r"\b(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}"
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    re.IGNORECASE,
)

_INPUT_DATE_TYPES = frozenset({"date", "datetime-local", "month", "week", "time"})
_INPUT_BUTTON_TYPES = frozenset({"submit", "button", "reset", "image"})

_MAX_NAME = 500
_MAX_ANCHOR = 500
_MAX_OPTION = 200
_MAX_OPTIONS = 60


# ─── Record shape ─────────────────────────────────────────────────────────────


class AnchorRecord(TypedDict):
    """1:1 with the substrate ``AnchorBundle`` (qe-central schema.py) and
    ``evidence_signals.anchor`` (writer.py:172-173)."""

    label: str
    kind: str


class ControlRecord(TypedDict):
    """One inventoried control.

    TOP-LEVEL fields are the compiler-bound manifest vocabulary (design §3.2);
    ``qec`` is the diagnostics-only bucket persisted verbatim under
    ``evidence_signals.qec`` (writer.py:170) and never bound on.
    """

    name: str
    name_source: str
    best_effort_name: bool
    role: str
    kind: str
    tag: str
    input_type: str
    options: list[str]
    #: How many options the control offers in the page. Greater than
    #: ``len(options)`` only when the read was clipped — the honest marker that
    #: the captured enumeration is a PREFIX, not the answer set.
    options_total: int
    required: bool
    disabled: bool
    #: Declared value constraints (number/range/date inputs); "" when undeclared.
    min: str
    max: str
    step: str
    #: Drag-and-drop signal (HTML5 draggable / ARIA grab); matcher → UNHANDLED.
    draggable: bool
    roledescription: str
    value_committed: str
    frame_selector: str
    #: The mutually-exclusive CHOICE GROUP this control answers, as DECLARED by
    #: the DOM (radio ``name`` attribute / ``role=radiogroup`` container); "" when
    #: the control is not part of one.  Structure only — which question, never
    #: what was answered.
    group_key: str
    #: Stamped by GROUP_ASSEMBLE on every member of a group of 2+ (absent
    #: otherwise): stable id of the question, the answers it offers, and how many
    #: members carry it.  ``group_options`` is deliberately separate from
    #: ``options`` so a radio's field signature does not shift when a sibling
    #: appears or disappears.
    group_id: str
    group_options: list[str]
    group_size: int
    anchor: Optional[AnchorRecord]
    match_index: Optional[int]
    danger: bool
    danger_rule_id: str
    danger_severity: str
    qec: dict[str, Any]


# ─── Small helpers ─────────────────────────────────────────────────────────────


def _s(value: Any) -> str:
    return "" if value is None else str(value)


#: How trustworthy a control's accessible name is, graded by WHICH name rung
#: produced it (U0). This is the escalate-to-vision signal: a visibly-interactive
#: page whose controls are mostly low/none confidence is DOM-opaque (canvas /
#: unlabelled custom widgets) and should be perceived visually (U2).
_NAME_CONFIDENCE = {
    "label-for": "high",        # explicit programmatic label association
    "aria-labelledby": "high",
    "aria-label": "high",
    "wrapping-label": "medium",  # structural label / element text
    "content": "medium",
    "title": "low",             # tooltip / placeholder — best-effort only
    "placeholder": "low",
}


def name_confidence_for(name_source: Any, name: Any) -> str:
    """Grade the accessible name's trustworthiness: high | medium | low | none.

    ``none`` when there is no name at all; otherwise by the rung in
    ``name_source``. Pure and value-free — the rung is UI shape, never a value.
    """
    if not str(name or "").strip():
        return "none"
    return _NAME_CONFIDENCE.get(str(name_source or "").strip(), "none")


def name_confidence_summary(controls: Iterable[Any]) -> dict[str, int]:
    """Per-page rollup of name confidence (U0 telemetry, U2 escalation signal).

    Counts controls by confidence tier. When a visibly-interactive page yields
    mostly low/none-confidence controls (or too few), the walk escalates to the
    vision Perceiver (U2). Pure.
    """
    tiers = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for c in controls:
        if not isinstance(c, dict):
            continue
        conf = str((c.get("qec") or {}).get("name_confidence") or "none")
        tiers[conf if conf in tiers else "none"] += 1
    return tiers


def _norm(text: Any) -> str:
    """Whitespace-collapsed, lower-cased — the identity used for collisions."""
    return " ".join(_s(text).split()).lower()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _as_int(value: Any) -> int:
    """A non-negative int from page-supplied JSON (0 for anything unreadable).

    Page-authored data is never trusted to be well-formed: a missing field, a
    string, a float or a negative all collapse to 0, and the caller floors the
    result at the captured length so the count can never claim FEWER options than
    were actually read.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    try:
        return max(int(float(str(value).strip())), 0)
    except (TypeError, ValueError):
        return 0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on", "required", "disabled")
    return bool(value)


def _clean_options(raw: Any) -> list[str]:
    out: list[str] = []
    for opt in raw or []:
        text = _clip(_s(opt).strip(), _MAX_OPTION)
        if text:
            out.append(text)
        if len(out) >= _MAX_OPTIONS:
            break
    return out


# ─── Kind refinement (mirrors compiler intent + DOM semantics) ─────────────────


def refine_kind(
    *, role: str, tag: str, input_type: str, options: Iterable[str], value: str,
) -> str:
    """Refine a raw control into the compiler ``observed.kind`` vocabulary.

    Deterministic and DOM-grounded.  Order matters: an explicit interactive
    role/type wins, then the option/date heuristics the compiler itself uses
    (``_refine_kind``, compiler.py:174-208) for ambiguous text-ish controls.
    """
    role = _norm(role)
    tag = _norm(tag)
    it = _norm(input_type)
    opts = list(options or [])

    if role == "switch":
        return "toggle"
    if role == "checkbox" or it == "checkbox":
        return "checkbox"
    if role == "radio" or it == "radio":
        return "radio"
    if role in ("combobox", "listbox") or tag == "select":
        return "select"
    if role == "link" or tag == "a":
        return "link"
    if (role in ("button", "menuitem", "menuitemcheckbox", "menuitemradio", "tab")
            or tag in ("button", "summary") or it in _INPUT_BUTTON_TYPES):
        return "button"
    if it in _INPUT_DATE_TYPES:
        return "date"
    # A slider / color is a NON-TEXT value control — typing a string into it is invalid
    # and must never be reported as a completed fill (a live green-wash: 'autotest' into a
    # range). Typed distinctly so the filler refuses it and the ledger flags it honestly.
    if role == "slider" or it == "range":
        return "slider"
    if it == "color":
        return "color"
    if tag in ("input", "textarea") or role in ("textbox", "searchbox", "spinbutton"):
        # Mirror the compiler's ambiguous-field heuristics (compiler.py:204-208):
        # >=2 captured options ⇒ a select rendered as a field; a date-shaped value
        # ⇒ date; otherwise plain text.
        if len(opts) >= 2:
            return "select"
        if _DATE_RX.search(value or ""):
            return "date"
        return "text"
    if len(opts) >= 2:
        return "select"
    # An unknown [role]/[tabindex] element that is actionable — treat as a button
    # (the compiler's default click branch), never silently a form field.
    return "button"


def target_kind_for(record: ControlRecord) -> str:
    """Map a record onto the ``page_actions.target_kind`` vocabulary."""
    role = _norm(record.get("role"))
    if role == "menuitem":
        return "menu_item"
    if role == "tab":
        return "tab"
    return _TARGET_KIND_BY_KIND.get(record.get("kind", ""), "other")


def form_signal_for(record: ControlRecord) -> Optional[dict[str, Any]]:
    """``form_snapshot_signals[label]`` value for a value-bearing control, or
    ``None`` for a button/link (not a form field).

    Shape ``{type, options, required}`` — exactly what ``build_field_meta``
    reads (compiler.py:153-171).
    """
    signal_type = _FORM_SIGNAL_TYPE_BY_KIND.get(record.get("kind", ""))
    if signal_type is None:
        return None
    sig = {
        "type": signal_type,
        "options": list(record.get("options") or []),
        "required": bool(record.get("required")),
    }
    # A DEPENDENT control (its options only populate after a driver field is chosen) carries
    # the driver's label — set by the crawler's ACT-THEN-DIFF pass. Keeps a downstream
    # consumer honest that the captured options are for ONE branch of the driver, not fixed.
    dep = record.get("depends_on")
    if dep:
        sig["depends_on"] = str(dep)
    # THE RULE THE APPLICATION DECLARED ABOUT ITSELF, carried to the one place
    # that reads it. The browser extractor has always captured min/max/step, the
    # control record has always held them, and this function — the boundary
    # qe-central reads validation from — dropped every one of them. Live, that
    # left `validation` NULL on all 24 catalogued questions including a Face
    # Amount input declaring step=10000: the clearest boundary rule on the form,
    # and no boundary scenario could be derived from it because the catalogue
    # never learned it. A question with no declared rule justifies no negative
    # and no boundary case, so this silence cost the scenario deriver its input
    # on every text and number field in the fleet.
    for key in _VALIDATION_KEYS:
        val = _s(record.get(key)).strip()
        if val:
            sig[key] = val[:80]
    return sig


# ─── Danger classification (delegates to the fail-closed guard) ────────────────

#: Kinds/roles that can actually ACTUATE something (and so carry an action verb).
#: A text field named "Delete reason" is not an actuator — never a never-click.
_ACTUATOR_KINDS = frozenset({"button", "link"})
_ACTUATOR_ROLES = frozenset({"button", "link", "menuitem", "menuitemcheckbox",
                             "menuitemradio", "tab"})


def _iter_irreversible_patterns(refuse_pack: Any) -> Iterable[tuple[str, "re.Pattern[str]"]]:
    """Yield ``(rule_id, compiled_regex)`` for the ImportError-only fallback.

    Accepts either the guard's ``RefusePack`` (``.irreversible_verbs``) or a raw
    parsed-YAML ``dict``.  Only used when :mod:`app.guard` cannot be imported at
    all (it ships with the explorer, so this is defensive).  A rule applies to a
    control name when ``applies_to`` is absent OR names a control/button surface.
    """
    rules = getattr(refuse_pack, "irreversible_verbs", None)
    if rules is None and isinstance(refuse_pack, dict):
        rules = refuse_pack.get("irreversible_verbs")
    name_surfaces = {"button_name", "action_button_name", "name", "button", "control"}
    for rule in rules or []:
        get = (lambda k: rule.get(k)) if isinstance(rule, dict) else (lambda k: getattr(rule, k, None))
        rule_id = _s(get("id")).strip()
        match = _s(get("match")).strip()
        applies = get("applies_to")
        if isinstance(applies, str):
            applies = [applies]
        if applies and not (name_surfaces & {str(a).strip().lower() for a in applies}):
            continue
        if not match:
            continue
        try:
            yield (rule_id or match, re.compile(match, re.IGNORECASE))
        except re.error as exc:
            logger.warning("qec.inventory.bad_refuse_pattern rule_id=%s error=%s",
                           rule_id, str(exc)[:200])


def _local_verb_danger(name: str, refuse_pack: Any) -> tuple[bool, str, str]:
    """Fail-closed self-contained danger check (guard-unimportable fallback).

    With no usable refuse pack NOTHING can be proven safe, so an actionable
    control is treated as an irreversible never-click — matching the guard's
    posture (``classify_action_verb`` returns ``irreversible=True`` for a
    non-``RefusePack``)."""
    if refuse_pack is None:
        return True, "no_refuse_pack", "critical"
    for rule_id, pattern in _iter_irreversible_patterns(refuse_pack):
        if pattern.search(name or ""):
            return True, rule_id, "critical"
    return False, "", ""


def _normalise_verb_result(result: Any) -> tuple[bool, str, str]:
    """Normalise ``classify_action_verb``'s return into ``(danger, rule_id,
    severity)``.

    The verified guard returns a :class:`~app.guard.VerbClassification`
    (``.irreversible`` / ``.rule_id`` / ``.severity``, guard.py:306-315).  A
    ``GuardDecision``-shaped (``.allow``) or tuple/bool return is also tolerated
    so a guard refactor cannot silently break the danger flag.
    """
    if hasattr(result, "irreversible"):
        return (bool(result.irreversible), _s(getattr(result, "rule_id", "")).strip(),
                _s(getattr(result, "severity", "")).strip())
    if hasattr(result, "allow"):
        return ((not bool(result.allow)), _s(getattr(result, "rule_id", "")).strip(),
                _s(getattr(result, "severity", "")).strip())
    if isinstance(result, tuple) and result:
        rid = _s(result[1]).strip() if len(result) > 1 else ""
        return (bool(result[0]), rid, "")
    if isinstance(result, bool):
        return (result, "", "")
    logger.warning("qec.inventory.unknown_verb_result type=%s", type(result).__name__)
    return False, "", ""


def classify_control_danger(
    name: str, kind: str, role: str, refuse_pack: Any, url: str = "",
) -> tuple[bool, str, str]:
    """Flag a control that actuates an irreversible action (delete / pay / …).

    Returns ``(danger, rule_id, severity)``.  Only ACTIONABLE controls are
    classified; everything else is trivially non-dangerous.  Delegates to the
    fail-closed :func:`app.guard.classify_action_verb` (the single source of
    truth for the refuse policy); the ``url`` lets url-path/query-scoped rules
    fire on a navigation-style control.  Falls back to a fail-closed local
    reader ONLY if the guard module cannot be imported at all.
    """
    if kind not in _ACTUATOR_KINDS and _norm(role) not in _ACTUATOR_ROLES:
        return False, "", ""
    target = (name or "").strip()
    if not target:
        # A nameless actuator cannot be verb-classified; fail-closed only when a
        # policy exists to compare against would be misleading — treat as unknown
        # (the a11y-weakness/no-name flag already surfaces the risk downstream).
        return False, "", ""
    try:
        from .guard import classify_action_verb  # type: ignore
    except Exception:
        classify_action_verb = None  # ships with the explorer; defensive only
    if classify_action_verb is not None:
        try:
            return _normalise_verb_result(classify_action_verb(target, url, refuse_pack))
        except Exception as exc:  # never let a guard error SUPPRESS a real danger
            logger.warning(
                "qec.inventory.guard_verb_failed fail-closed local reader: %s",
                str(exc)[:200],
            )
    return _local_verb_danger(target, refuse_pack)


# ─── Anchor disambiguation (only on collision) ─────────────────────────────────


def _anchor_from_landmark(landmark: Any) -> Optional[AnchorRecord]:
    """Map a raw ``landmark {role, name}`` onto an :class:`AnchorRecord`.

    The landmark's ARIA role picks the ``_ANCHOR_ROLE`` scope; an unrecognised
    landmark degrades to the text-based ``block`` scope (compiler.py:264-278).
    Returns ``None`` when the landmark has no identifying name — an
    un-anchorable collision is honest, never a fabricated locator.
    """
    if not isinstance(landmark, dict):
        return None
    name = _s(landmark.get("name")).strip()
    if not name:
        return None
    role = _norm(landmark.get("role"))
    kind = _ANCHOR_KIND_BY_LANDMARK.get(role, "block")
    return {"label": _clip(name, _MAX_ANCHOR), "kind": kind}


def _collision_key(record: ControlRecord) -> tuple[str, str, str]:
    """Controls collide (and thus need disambiguation) when they would compile
    to the SAME locator: same owning frame, same role, same accessible name.

    ``frame_selector`` is in the key so a control duplicated across a main frame
    and an iframe is NOT treated as ambiguous — the iframe scope already
    disambiguates it (compiler.py:254-259)."""
    return (record.get("frame_selector", ""), _norm(record.get("role")), _norm(record.get("name")))


# ─── Public entry point ────────────────────────────────────────────────────────


def build_control_record(
    raw: dict[str, Any], refuse_pack: Any = None, *, url: str = "",
) -> ControlRecord:
    """Refine ONE raw control (without collision-based anchoring).

    ``url`` is the page location the control was seen on — passed to the guard
    so url-path/query-scoped refuse rules can fire on a navigation-style
    control; ``""`` degrades safely to name-only classification.
    """
    role = _norm(raw.get("role"))
    tag = _norm(raw.get("tag"))
    input_type = _norm(raw.get("input_type"))
    name = _clip(_s(raw.get("name")).strip(), _MAX_NAME)
    options = _clean_options(raw.get("options"))
    value_committed = _s(raw.get("value_committed"))
    frame_selector = _s(raw.get("frame_selector")).strip()

    kind = refine_kind(
        role=role, tag=tag, input_type=input_type,
        options=options, value=value_committed,
    )
    # A URL-SCOPED REFUSE RULE MATCHES WHERE A CONTROL GOES, NOT WHERE IT SITS.
    #
    # The PAGE url was passed here, so a rule with applies_to: [url_path] fired
    # for EVERY actuator rendered on a matching page. Reproduced exactly:
    # rp.verb.underwrite matches \bunderwriting\b, so on
    # /underwriting/new-business/new-application every single control came back
    # danger=critical — the Back button, the user avatar, the notification bell
    # labelled "3", the wizard's own step tabs, and Continue itself (20 of 35
    # controls on one page).
    #
    # That is not a safety property, it is a blind spot: every advance tier skips
    # danger controls, so the funnel became unwalkable, AND _tier3_candidates
    # emptied — which is why the agent oracle recorded zero consultations
    # fleet-wide. One over-broad rule produced both symptoms.
    #
    # A control's DESTINATION is its own href; a button has none, so a url rule
    # simply does not apply to it and its LABEL remains the only signal — which
    # is what button_name is for, and still catches "Submit to Underwriting".
    # Nothing is weakened: a link TO a dangerous route is still matched on that
    # route, nameless actuators are already unclassifiable, the EXPLORE-phase
    # network guard still blocks every mutation, and the submit tier still
    # requires attestation plus approval.
    control_dest = _s(raw.get("href")).strip()
    if control_dest.startswith(("javascript:", "#", "mailto:", "tel:")):
        control_dest = ""
    danger, danger_rule_id, danger_severity = classify_control_danger(
        name, kind, role, refuse_pack, control_dest,
    )

    record: ControlRecord = {
        "name": name,
        "name_source": _s(raw.get("name_source")) or "none",
        "best_effort_name": bool(raw.get("best_effort")) or (not name),
        "role": role,
        "kind": kind,
        "tag": tag,
        "input_type": input_type,
        "options": options,
        # How many options the control ACTUALLY offers, as counted in the page.
        # Equal to len(options) unless the injected-JS ceiling clipped the read.
        # Carried so a CLIPPED enumeration can never be catalogued as the complete
        # set of answers to a question: "247 offered, 300 captured" is a fact a
        # consumer can act on; a silently-shortened list is a fabrication.
        "options_total": max(_as_int(raw.get("options_total")), len(options)),
        "required": _as_bool(raw.get("required")),
        "disabled": _as_bool(raw.get("disabled")),
        # Declared value constraints (number/range/date inputs) — the DOM's own
        # truth, consumed by the default synthesizer so an auto-filled value can
        # never violate the app's min/max/step validation.
        "min": _s(raw.get("min")).strip(),
        "max": _s(raw.get("max")).strip(),
        "step": _s(raw.get("step")).strip(),
        "pattern": _s(raw.get("pattern")).strip(),
        "minlength": _s(raw.get("minlength")).strip(),
        "maxlength": _s(raw.get("maxlength")).strip(),
        # Drag-and-drop signal → matcher names it UNHANDLED (blind spot, ledgered).
        "draggable": _as_bool(raw.get("draggable")),
        "roledescription": _s(raw.get("roledescription")).strip(),
        "value_committed": value_committed,
        "frame_selector": frame_selector,
        # DOM-declared choice grouping; GROUP_ASSEMBLE (pass 3) turns this into
        # group_id/group_options once it knows the control's siblings.
        "group_key": _s(raw.get("group_key")).strip(),
        "anchor": None,   # filled by build_inventory only on collision
        "match_index": None,  # DOM ordinal among identical controls (collision only)
        "danger": danger,
        "danger_rule_id": danger_rule_id,
        "danger_severity": danger_severity,
        # Diagnostics-only bucket → evidence_signals.qec (writer.py:170); note
        # input_type is REQUIRED here so the writer can flag password secrets
        # (writer.action_is_secret reads qec.input_type, writer.py:119).
        "qec": {
            "role": role,
            "testid": _s(raw.get("testid")),
            "css_hint": _s(raw.get("css_hint")),
            "input_type": input_type,
            "options": options,
            "frame_selector": frame_selector,
            # Link destination (anchors only) — diagnostics-only; the crawler reads
            # it to FOLLOW routes directly (href-follow traversal). No compiler rung.
            "href": _s(raw.get("href")).strip(),
            # aria-haspopup — marks a hover/menu trigger the crawler hovers to reveal
            # a fly-out menu; diagnostics-only, no compiler rung.
            "haspopup": _s(raw.get("haspopup")).strip(),
            # aria-expanded — marks a CLICK-to-open dropdown/disclosure toggle the
            # crawler clicks to reveal hidden menu items; diagnostics-only.
            "expanded": _s(raw.get("expanded")).strip(),
            # U0 — accessible-name confidence (high|medium|low|none), graded by the
            # name rung. The escalate-to-vision signal for DOM-opaque pages.
            "name_confidence": name_confidence_for(raw.get("name_source"), name),
        },
    }
    return record


def build_inventory(
    raw_controls: Iterable[dict[str, Any]], refuse_pack: Any = None, *, url: str = "",
) -> list[ControlRecord]:
    """Refine raw browser controls into compiler-bound :class:`ControlRecord`\\ s.

    Two passes:
      1. refine every control independently (kind, danger flag, qec bucket);
      2. attach a disambiguation ``anchor`` to EACH control whose
         ``(frame, role, name)`` collides with another — mirroring the
         compiler's ``_ANCHOR_ROLE`` scoping (compiler.py:216-280).  The anchor
         label/kind comes from the control's nearest landmark ancestor (carried
         on the raw control by :data:`app.inventory_js.INVENTORY_JS`).  A
         control that is unique on its page carries no anchor, and a colliding
         control with no identifiable landmark carries none either — an
         un-anchorable collision is honest evidence, never a fabricated locator.

    ``url`` is the page location (threaded to the guard for url-scoped refuse
    rules).  Order is preserved (crawl/document order) so the manifest is
    deterministic and re-crawls of an unchanged page produce byte-identical
    control lists.
    """
    raws = [dict(raw or {}) for raw in (raw_controls or [])]
    records = [build_control_record(raw, refuse_pack, url=url) for raw in raws]

    counts: dict[tuple[str, str, str], int] = {}
    for rec in records:
        key = _collision_key(rec)
        counts[key] = counts.get(key, 0) + 1

    anchored = 0
    ordinal_in_key: dict[tuple[str, str, str], int] = {}
    for raw, rec in zip(raws, records):
        key = _collision_key(rec)
        if counts.get(key, 0) < 2:
            continue
        # POSITIONAL FALLBACK. A set of identical controls (same frame/role/name)
        # with no distinguishing landmark cannot be ANCHORED — but it can still be
        # targeted by ORDER: the k-th such control in DOM order. Stamp that ordinal
        # so the locator resolves get_by_role(...).nth(k) instead of always .first.
        # This is the ONLY handle on a bare-button questionnaire (17 identical "Yes"
        # buttons, one per question, no aria/landmark/testid). The inventory walks
        # the DOM in order, so the k-th record here IS the k-th DOM match.
        idx = ordinal_in_key.get(key, 0)
        rec["match_index"] = idx
        ordinal_in_key[key] = idx + 1
        anchor = _anchor_from_landmark(raw.get("landmark"))
        if anchor is not None:
            rec["anchor"] = anchor
            anchored += 1

    # Pass 3: GROUP_ASSEMBLE — a set of mutually-exclusive radios is ONE
    # question with N answers, not N unrelated toggles.
    #
    # Grouping is the DOM's own declaration, never a heuristic: native radios
    # group by their ``name`` attribute scoped to the owning form, ARIA ones by
    # their ``role=radiogroup`` container (see ``groupKeyOf`` in inventory_js).
    # A page with a product picker AND a gender picker therefore yields two
    # groups, not one — grouping merely by "same frame" would fuse unrelated
    # questions and offer each the other's answers.
    #
    # Each member is stamped with:
    #   ``group_id``      — stable identity of the QUESTION.  The decision-point
    #                       ledger keys branches on this, so N members × N
    #                       options collapse to one decision with N branches
    #                       instead of an N² cross-product.
    #   ``group_options`` — every answer the question offers, in DOM order.
    #   ``group_size``    — how many members the question has.
    #
    # Deliberately NOT written to ``options``: that field feeds the field
    # signature's option-shape bucket, and a radio's identity must not change
    # just because a sibling appeared.  ``group_options`` is carried alongside
    # so enumeration can see the answers without churning signatures.
    grouped = groups_found = 0
    by_group: dict[tuple[str, str, str], list[int]] = {}
    for idx, rec in enumerate(records):
        # Radio OR checkbox: a mutually-exclusive choice and a multi-select are
        # both ONE question. Only DECLARED groupings reach here at all (see
        # groupKeyOf), so a lone consent checkbox never acquires a group.
        if rec.get("kind") not in ("radio", "checkbox"):
            continue
        group_key = _s(rec.get("group_key")).strip()
        if not group_key:
            continue           # undeclared grouping → left exactly as before
        # Kind is part of the key: a <fieldset> holding both radios and
        # checkboxes yields ONE container key, and merging those into a single
        # question would enumerate answers that belong to different questions.
        by_group.setdefault(
            (rec.get("frame_selector") or "", group_key, str(rec.get("kind"))),
            []).append(idx)

    for (frame, group_key, kind), indices in by_group.items():
        if len(indices) < 2:
            continue           # a lone radio is a toggle, not a question
        options = [records[i]["name"] for i in indices if records[i].get("name")]
        if len(options) < 2:
            continue           # unnameable members → nothing honest to enumerate
        # Radio group_ids keep their historical hash input EXACTLY — they key
        # remembered branch-walk overrides across crawls, and re-hashing them
        # would silently orphan every plan a previous crawl recorded. Checkboxes
        # are new and take their own namespace.
        seed = (f"{frame}\x1f{group_key}" if kind == "radio"
                else f"{frame}\x1f{group_key}\x1f{kind}")
        group_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
        for i in indices:
            records[i]["group_id"] = group_id
            records[i]["group_options"] = options
            records[i]["group_size"] = len(indices)
        groups_found += 1
        grouped += len(indices)

    logger.info(
        "qec.inventory.built controls=%d anchored=%d dangerous=%d "
        "radio_groups=%d radio_grouped=%d",
        len(records), anchored, sum(1 for r in records if r["danger"]),
        groups_found, grouped,
    )
    return records

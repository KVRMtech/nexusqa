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
from typing import Any, Iterable, Mapping, Optional, Sequence, TypedDict

from .inventory_js import MAX_OPTIONS

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

#: THE option ceiling, imported rather than restated.
#:
#: This layer used to carry its own ``60`` and silently re-truncated what the
#: walker had been raised to 300 specifically to preserve: a 250-country question
#: reached ``form_snapshot_signals`` — the thing the catalogue and the scenario
#: deriver actually read — as its first 60 answers. The enumeration a control
#: offers IS the test data for every positive, negative and boundary case derived
#: from that question, so a prefix presented as the whole answer set is a
#: fabricated one. There is now one number, and it lives in :mod:`app.inventory_js`.
_MAX_OPTIONS = MAX_OPTIONS


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
    #: WHAT THE APPLICATION DECLARED THIS FIELD IS FOR. ``autocomplete`` is a
    #: W3C-standard vocabulary and is the STRONGEST signal the classifier has —
    #: :func:`app.field_semantics.classify` weights it first, above every reading
    #: of a label. ``inputmode`` is a weaker declaration of the same kind.
    autocomplete: str
    inputmode: str
    #: The classifier's fallbacks for a control with NO accessible name:
    #: :func:`app.field_signature.compute` tokenises the placeholder, then the id.
    placeholder: str
    id: str
    #: Declared validation (the app's own rule about its own field); "" when
    #: undeclared. Read by :func:`form_signal_for` into the catalogue's
    #: ``validation`` block.
    pattern: str
    minlength: str
    maxlength: str
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
    #: THE QUESTION THIS CONTROL ANSWERS, as the DOM declared it (M2.1).
    #: ``question_key`` is the declared container (fieldset / role=radiogroup /
    #: role=group) — wider than ``group_key``, which only ever sees radios and
    #: checkboxes; ``question_label`` is the application's own wording for that
    #: question, from a declared accessible-name rung only, "" when the page
    #: declared none. ``question_group_id`` is stamped by QUESTION_ASSEMBLE on
    #: the members of a declared container holding 2+ controls that acquired NO
    #: ``group_id`` — a bare-<button> Yes/No pair is one question too, and until
    #: this existed the only identity it could be given was its DOM ordinal.
    question_key: str
    question_label: str
    question_label_source: str
    question_group_id: str
    anchor: Optional[AnchorRecord]
    match_index: Optional[int]
    #: CAPTURE-EVIDENCE LOCATOR (M2.2 / T-BR-03) — stamped by
    #: :func:`build_inventory` pass 4, which is the only place that can see the
    #: whole page and therefore the only place that can say whether a handle
    #: resolves to ONE control. ``None`` when the page declared no handle at all.
    locator: Optional[dict[str, Any]]
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


#: Kinds whose members are ANSWERS to one question rather than questions of
#: their own. A grouped <select> does not exist — a select IS its own question —
#: so folding is scoped to the kinds that spread one question across N elements.
_GROUPED_MEMBER_KINDS = frozenset({"radio", "checkbox", "toggle", "button"})


def question_identity(record: Mapping[str, Any]) -> str:
    """THE ONE id of the question a control answers, or "" when it answers a
    question of its own (M2.1 / T-QT-04).

    Two DOM declarations can group controls into a question and they never
    overlap: ``group_id`` (radio / checkbox sets, from HTML's own grouping) and
    ``question_group_id`` (any 2+ controls sharing a declared fieldset /
    role=group / role=radiogroup container — the bare-button questionnaire).
    Every consumer that asks "which question is this" must go through here, or
    the two declarations become two id spaces for one question, which is exactly
    the collision this milestone closes.
    """
    return (str(record.get("group_id") or "").strip()
            or str(record.get("question_group_id") or "").strip())


def question_groups_of(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The QUESTIONS a page asks, folded from the controls that answer them.

    One entry per declared question group, in first-sighting order:

    ``{group_id, label, label_source, type, options, options_total, required,
    members:[{name, kind}]}``

    THE HOLE THIS FILLS. ``form_snapshot_signals`` — the only control payload
    that has ever crossed to qe-central — is keyed by a control's ACCESSIBLE
    NAME, which for a radio group is the name of an ANSWER. A "Gender" question
    therefore reached the catalogue as two questions called "Male" and "Female",
    each offering no answers at all, and a 20-question health questionnaire whose
    every control is named "Yes" or "No" reached it as TWO questions, because a
    dict keyed by name cannot hold forty. The grouping was known in this process
    the whole time and simply had no way across.

    Value-free: labels and option text are product UI text, exactly like every
    other name in the manifest. No committed value enters this.
    """
    out: list[dict[str, Any]] = []
    by_gid: dict[str, dict[str, Any]] = {}
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        gid = question_identity(rec)
        if not gid:
            continue
        kind = str(rec.get("kind") or "")
        if kind not in _GROUPED_MEMBER_KINDS:
            continue
        name = _s(rec.get("name")).strip()
        row = by_gid.get(gid)
        if row is None:
            row = {
                "group_id": gid,
                # The application's OWN wording, or "" — never a substitute.
                "label": _s(rec.get("question_label")).strip()[:_MAX_NAME],
                "label_source": _s(rec.get("question_label_source")).strip()[:40],
                "type": kind,
                "options": [],
                "required": False,
                "members": [],
            }
            by_gid[gid] = row
            out.append(row)
        elif not row["label"]:
            # Any member may be the one that saw the container's wording.
            row["label"] = _s(rec.get("question_label")).strip()[:_MAX_NAME]
            row["label_source"] = _s(rec.get("question_label_source")).strip()[:40]
        row["required"] = bool(row["required"]) or bool(rec.get("required"))
        if name and name not in row["options"]:
            row["options"].append(name)
        if name and not any(m.get("name") == name for m in row["members"]):
            row["members"].append({"name": name[:_MAX_NAME], "kind": kind})
    for row in out:
        # ``group_options`` is the DOM order the grouping pass recorded; prefer
        # it when it is at least as complete, so the answers read in page order.
        row["options_total"] = len(row["options"])
    return out


def form_signal_for(record: ControlRecord) -> Optional[dict[str, Any]]:
    """``form_snapshot_signals[label]`` value for a value-bearing control, or
    ``None`` for a button/link (not a form field).

    Shape ``{type, options, required}`` — exactly what ``build_field_meta``
    reads (compiler.py:153-171).
    """
    signal_type = _FORM_SIGNAL_TYPE_BY_KIND.get(record.get("kind", ""))
    if signal_type is None:
        return None
    options = list(record.get("options") or [])
    if not options:
        # A RADIO'S ANSWERS ARE ITS SIBLINGS, NOT ITS CHILDREN.
        #
        # MEASURED (health-declaration fixture, 2026-08-29): a 25-question Yes/No
        # health page produced a bundle where every question reported `opts=0 []`
        # -- no answers at all -- while the same run's inventory reported
        # `radio_groups=25 radio_grouped=50`, so all fifty options were seen.
        #
        # The browser fills `options` for a <select>, whose answers are its own
        # children. A radio group's answers are recorded by the grouping pass in
        # `group_options` ("every answer the question offers, in DOM order"), and
        # this function -- the only boundary qe-central reads a question's
        # answers from -- never looked there. Every radio question in every crawl
        # this product has run reached the catalogue with an empty answer list,
        # and `options_total` (the marker that exists so a CLIPPED enumeration
        # can never be read as complete) reported 0 for a complete one.
        #
        # A <select> is unaffected: it has its own options and no group.
        options = list(record.get("group_options") or [])
    sig = {
        "type": signal_type,
        "options": options,
        # HOW MANY ANSWERS THE QUESTION OFFERS — not how many survived the read.
        # The browser has always counted this (inventory_js.optionsAndTotalOf)
        # and the control record has always carried it, and this function — the
        # ONLY boundary qe-central reads a question's answers from — did not pass
        # it on. Downstream, ``catalog._options_total`` floors the total at the
        # number stored, so with nothing declared the catalogue reported exactly
        # as many answers as it happened to keep: a clipped 250-option country
        # list came out as a complete 300, and a scenario deriver that refuses to
        # claim completeness on a clipped enumeration could not tell it was one.
        # The honesty marker existed at both ends of the wire and nothing carried
        # it across.
        "options_total": max(_as_int(record.get("options_total")), len(options)),
        "required": bool(record.get("required")),
    }
    # THE HANDLE THE PAGE DECLARED FOR THIS CONTROL (M2.2 / T-BR-03). Captured
    # since M0.x as ``testid`` / ``css_hint`` / the accessible name, verified for
    # uniqueness by :func:`attach_locators`, and until now readable only inside
    # the explorer: the catalogue described a question in full and could not say
    # which element on the page asks it. Carried whole — including an UNVERIFIED
    # verdict, which is a finding about the application (a control it identifies
    # by nothing) and not an absence to hide.
    locator = record.get("locator")
    if isinstance(locator, dict) and locator:
        sig["locator"] = dict(locator)
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


# ─── Carrying what a re-read cannot recover (M2.2 / T-BR-02) ──────────────────

#: Annotations that are CRAWL FINDINGS, not DOM readings. Every one of them was
#: established by DOING something — acting on a driver and diffing the page,
#: opening a menu that builds itself on open — and none of them can be recovered
#: by inventorying the page again, because the page never stated them.
_EARNED_KEYS = ("depends_on", "options_total", "options_truncated")


def carry_earned_annotations(
    previous: Iterable["ControlRecord"], fresh: list["ControlRecord"],
) -> list["ControlRecord"]:
    """Re-apply to ``fresh`` the findings ``previous`` earned. Mutates + returns.

    THE DEFECT THIS CLOSES, found by crawling a real application.  The discovery
    pass proves a dependency the way it can only be proven — commit a driver act,
    re-observe, watch a second question's answer set change — and writes it onto
    the control.  A later step then re-inventories the page (the unblock
    experiment does, because it must re-read the page to see whether the
    application enabled its forward control) and REPLACES the snapshot with the
    fresh read.  The fresh read is correct about everything the DOM says and
    silent about everything it does not, so the dependency vanished between the
    step that proved it and the record that leaves the crawl.  Live, the
    catalogue's ``depends_on`` was empty on every question in the fleet, and the
    pass that produces it looked like it had simply never found anything.

    The same applies to an enumeration read by OPENING a custom menu: re-reading
    the closed page yields nothing, and taking the fresh silence would trade a
    known answer set for an empty one.

    RICHER WINS, NEVER POORER.  A fresh read that genuinely saw MORE keeps what
    it saw — this only fills silence, so a page that really did change is still
    described by the newest observation.
    """
    by_name: dict[str, "ControlRecord"] = {}
    for rec in previous or ():
        name = _norm(rec.get("name"))
        if name and name not in by_name:
            by_name[name] = rec
    for rec in fresh:
        prior = by_name.get(_norm(rec.get("name")))
        if prior is None:
            continue
        for key in _EARNED_KEYS:
            if not rec.get(key) and prior.get(key):
                rec[key] = prior[key]
        prior_options = list(prior.get("options") or ())
        if len(prior_options) > len(rec.get("options") or ()):
            rec["options"] = prior_options
        qec, prior_qec = rec.get("qec"), prior.get("qec")
        if isinstance(qec, dict) and isinstance(prior_qec, dict):
            if rec.get("depends_on"):
                qec["depends_on"] = rec["depends_on"]
            if len(prior_options) > len(qec.get("options") or ()):
                qec["options"] = prior_options
    return fresh


# ─── Locator evidence (M2.2 / T-BR-03) ────────────────────────────────────────

#: Ceiling on any single locator string. A locator is a HANDLE, not a document;
#: a page that emits a 4KB generated class list must not be able to inflate every
#: catalogue row by it.
_MAX_LOCATOR = 200

#: The handles a page can DECLARE about one control, strongest first. Every one
#: of these is read straight off the DOM — there is no rung here that composes a
#: selector the application never offered. That is the whole discipline of
#: T-BR-03: a catalogue may report the handle a page gave, and must report
#: nothing when the page gave none, because a manufactured selector is a claim
#: about an application that no crawl ever tested.
#:
#: ``accessible_name`` sits below the two data attributes and above ``css_hint``
#: for a reason that is not about strength but about MEANING: it is the only rung
#: the deterministic compiler actually binds on (compiler.py:297-331), so a
#: catalogue question whose locator is an accessible name is one a generated
#: script can target unchanged. ``css_hint`` never can — it is recorded as
#: evidence that SOMETHING identifies the control, and marked as the diagnostic
#: it is.
LOCATOR_TESTID = "testid"
LOCATOR_DOM_ID = "dom_id"
LOCATOR_ACCESSIBLE_NAME = "accessible_name"
LOCATOR_CSS_HINT = "css_hint"

#: Why a control has no verified locator. Recorded rather than left blank: "we
#: could not identify this control" and "we did not look" are different findings,
#: and only one of them is the application's fault.
LOCATOR_UNVERIFIED_NO_HANDLE = "no_handle_declared"
LOCATOR_UNVERIFIED_AMBIGUOUS = "ambiguous_in_page"


def _dom_id_of(record: "ControlRecord") -> str:
    return _s(record.get("id")).strip()


def _testid_of(record: "ControlRecord") -> str:
    qec = record.get("qec")
    return _s(qec.get("testid")).strip() if isinstance(qec, dict) else ""


def _css_hint_of(record: "ControlRecord") -> str:
    qec = record.get("qec")
    return _s(qec.get("css_hint")).strip() if isinstance(qec, dict) else ""


def attach_locators(records: list["ControlRecord"]) -> int:
    """Stamp each control with the locator its page DECLARED. Returns how many
    came out VERIFIED.

    Uniqueness is why this cannot live in :func:`build_control_record`: a handle
    is only a locator if it resolves to ONE control, and that is a property of
    the PAGE, not of the element. One control examined alone can report the id it
    carries; only the page can say whether another control carries it too —
    and ids DO collide in practice (two shadow roots, a repeated row template,
    a component rendered twice). A per-control pass would have had to either
    assume uniqueness — which is the fabrication this task exists to prevent —
    or never claim it, which would make every locator unverified and the field
    worthless.

    Scoping is per FRAME: the compiler enters an iframe before resolving
    anything inside it (compiler.py:254-259), so the same id in the main
    document and in an embedded one is not a collision.

    A control whose name collides is still verified: :func:`build_inventory` has
    already stamped the DOM ordinal that separates it from its twins, and the
    k-th match in document order is exactly as real a handle as a data-testid.
    What is NOT verified is a control the page identified by nothing at all —
    no test attribute, no id, no accessible name — and that is recorded as the
    finding it is rather than papered over with a positional guess.
    """
    frames = [_s(r.get("frame_selector")) for r in records]
    testid_counts: dict[tuple[str, str], int] = {}
    dom_id_counts: dict[tuple[str, str], int] = {}
    css_counts: dict[tuple[str, str], int] = {}
    for frame, rec in zip(frames, records):
        for value, counts in ((_testid_of(rec), testid_counts),
                              (_dom_id_of(rec), dom_id_counts),
                              (_css_hint_of(rec), css_counts)):
            if value:
                counts[(frame, value)] = counts.get((frame, value), 0) + 1

    verified = 0
    for frame, rec in zip(frames, records):
        loc: dict[str, Any] = {}
        testid, dom_id = _testid_of(rec), _dom_id_of(rec)
        css_hint, name = _css_hint_of(rec), _s(rec.get("name")).strip()
        if testid and testid_counts.get((frame, testid), 0) == 1:
            loc = {"strategy": LOCATOR_TESTID, "value": _clip(testid, _MAX_LOCATOR)}
        elif dom_id and dom_id_counts.get((frame, dom_id), 0) == 1:
            loc = {"strategy": LOCATOR_DOM_ID, "value": _clip(dom_id, _MAX_LOCATOR)}
        elif name:
            # The compiler's own rung. Verified whether or not the name is
            # unique, because a collision here has already been given an ordinal.
            loc = {"strategy": LOCATOR_ACCESSIBLE_NAME,
                   "value": _clip(name, _MAX_LOCATOR)}
        elif css_hint and css_counts.get((frame, css_hint), 0) == 1:
            # Diagnostics-grade: no compiler rung binds on it, so it is carried
            # as evidence the control is identifiable and NOT as something a
            # generated script may target.
            loc = {"strategy": LOCATOR_CSS_HINT,
                   "value": _clip(css_hint, _MAX_LOCATOR), "bindable": False}
        else:
            rec["locator"] = {
                "strategy": "", "value": "", "verified": False,
                "unverified_reason": (LOCATOR_UNVERIFIED_AMBIGUOUS
                                      if (testid or dom_id or css_hint)
                                      else LOCATOR_UNVERIFIED_NO_HANDLE),
            }
            continue

        loc["verified"] = True
        loc.setdefault("bindable", loc["strategy"] == LOCATOR_ACCESSIBLE_NAME)
        # SCOPE — everything the compiler needs to enter the right context before
        # it resolves the handle above. Each is present only when the page made
        # it true, so an absent key is evidence of a simple page, not a gap.
        role = _norm(rec.get("role"))
        if role:
            loc["role"] = role
        if frame:
            loc["frame_selector"] = _clip(frame, _MAX_LOCATOR)
        idx = rec.get("match_index")
        if isinstance(idx, int):
            loc["match_index"] = idx
        if isinstance(rec.get("anchor"), dict):
            loc["anchor"] = dict(rec["anchor"])
        # WHICH QUESTION A MEMBER ANSWERS. Four radios are one question with four
        # answers, and each answer is a different element: without this a reader
        # holding four locators cannot tell four members of one question from
        # four unrelated toggles, and a merge that "kept the richest" could put
        # one member's handle on another member's row without contradicting
        # anything. Structure only — never which answer was taken.
        group_id = _s(rec.get("group_id")).strip()
        if group_id:
            loc["group_id"] = group_id
        rec["locator"] = loc
        verified += 1
    return verified


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
        # THE APPLICATION'S OWN DECLARATION OF WHAT THIS FIELD IS FOR, carried to
        # the classifier that ranks it above everything else. The walker emits
        # these; this layer used to drop them, which left
        # field_semantics.classify() rung 1 (confidence 0.98, the app's own W3C
        # words) unreachable on every crawled control, and left the placeholder/id
        # token fallbacks for nameless fields as dead code. Normalised in the
        # walker for the enumerated keywords; id/placeholder stay verbatim.
        #
        # ONE-TIME CONSEQUENCE, deliberate: `autocomplete` and `inputmode` are
        # part of the field-signature hash material (field_signature.compute).
        # They have always been read there and have always arrived empty, so a
        # control that DECLARES either now hashes differently than it did before
        # this change. Learned priors and proven mechanics stored against the old
        # hash simply stop matching for those fields — they do not mis-match, and
        # both consumers fail open (classify falls through to its own rungs, the
        # ladder walks in full). SIGNATURE_VERSION is deliberately NOT bumped:
        # every control that declares neither attribute keeps its signature, so
        # the churn is confined to the fields that are now better characterised.
        "autocomplete": _s(raw.get("autocomplete")).strip(),
        "inputmode": _s(raw.get("inputmode")).strip(),
        "placeholder": _s(raw.get("placeholder")),
        "id": _s(raw.get("id")).strip(),
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
        # ── CONTROL-SCOPED VALIDITY ──────────────────────────────────────
        # Validity used to be a property of the PAGE: the fill path read every
        # visible [role=alert] on the document and took the first one as the
        # verdict on whatever control it had just typed into.  A cookie banner
        # (nearly always role=alert, so screen readers announce it) therefore
        # failed every fill on the page, and one real error on field 3 failed
        # fields 4 through 12 with it.
        #
        # These are the accessibility contract for exactly this question, and
        # every mainstream form library already emits them.  Carried so
        # `fill_engine.validation` can ANCHOR a message to the control it is
        # about, and record everything else as page context that fails nothing.
        #
        # VALUE-FREE: an error message says what the application demands, never
        # what anybody entered — the same class of string as a label.
        "aria_invalid": _s(raw.get("aria_invalid")).strip().lower(),
        "aria_describedby": _s(raw.get("aria_describedby")).strip(),
        "aria_errormessage": _s(raw.get("aria_errormessage")).strip(),
        "error_text": _clip(_s(raw.get("error_text")).strip(), _MAX_NAME),
        "validation_message": _clip(_s(raw.get("validation_message")).strip(),
                                    _MAX_NAME),
        # ── POSSESSOR CONTEXT ────────────────────────────────────────────
        # The heading this control sits under.  A real application labels the
        # group once ("Beneficiary Information") and the fields inside it
        # plainly ("First Name"), so a resolver that reads only the control's
        # own name answers every one of them with the applicant.  The nearest
        # landmark was already computed; it was simply discarded unless two
        # controls collided.  Product UI text, never a value.
        "section": _clip(_s(raw.get("section")).strip(), _MAX_ANCHOR),
        "value_committed": value_committed,
        "frame_selector": frame_selector,
        # DOM-declared choice grouping; GROUP_ASSEMBLE (pass 3) turns this into
        # group_id/group_options once it knows the control's siblings.
        "group_key": _s(raw.get("group_key")).strip(),
        # DOM-declared QUESTION identity + wording; QUESTION_ASSEMBLE (pass 3b)
        # turns question_key into question_group_id once it knows the siblings.
        "question_key": _s(raw.get("question_key")).strip(),
        "question_label": _clip(_s(raw.get("question_label")).strip(), _MAX_NAME),
        "question_label_source": _s(raw.get("question_label_source")).strip(),
        "question_group_id": "",
        "anchor": None,   # filled by build_inventory only on collision
        "match_index": None,  # DOM ordinal among identical controls (collision only)
        "locator": None,  # filled by build_inventory pass 4 (needs whole-page uniqueness)
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
            # M2.6 / T-CAP-03 — "collapsed" | "expanded" | "". The DOM's own
            # answer to "is this control a door, and is it shut", normalised
            # across <details>/aria-expanded/role=tab by capture (only capture
            # can see `details.open`, which is a property, not an attribute).
            # Read by the crawler's pre-capture expansion pass so a field behind
            # a collapsed accordion is catalogued; no compiler rung.
            "disclosure": _s(raw.get("disclosure")).strip(),
            # U0 — accessible-name confidence (high|medium|low|none), graded by the
            # name rung. The escalate-to-vision signal for DOM-opaque pages.
            "name_confidence": name_confidence_for(raw.get("name_source"), name),
            # M3.2 / T-FR-01 — HOW this control was reached. "" is the ordinary
            # in-page walk (main document, open/closed shadow roots, same-origin
            # frames); "cross_origin_frame" means the port entered a foreign
            # embed through Playwright's frame APIs and read it from inside.
            # Carried because a reader deciding whether to ACT on a control (a
            # vendor payment field is not an application form field) must be able
            # to tell without re-deriving it from the selector string.
            "capture_scope": _s(raw.get("capture_scope")).strip(),
            # scheme://host of that embed. ORIGIN, never the URL: a vendor frame
            # URL routinely carries a client secret in its query string.
            "frame_origin": _s(raw.get("frame_origin")).strip(),
            # M3.2 / T-FR-02 — "" or "closed_shadow". A control the capture hook
            # observed inside a CLOSED shadow root. Orthogonal to capture_scope:
            # a closed root can sit inside a cross-origin frame and both facts
            # are true at once.
            #
            # IT IS CARRIED BECAUSE IT LIMITS WHAT MAY BE CLAIMED. Playwright's
            # selector engine pierces OPEN shadow roots by reading
            # `element.shadowRoot`, which is null for a closed one — for
            # Playwright exactly as for the page. So this control is real,
            # catalogued evidence of a question the application asks, and NO
            # standard locator can bind it. Recording it without recording that
            # would be a capture-says-covered / replay-cannot-bind claim.
            "shadow_scope": _s(raw.get("shadow_scope")).strip(),
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

    # Pass 3b: QUESTION_ASSEMBLE — the questions the choice-grouping above
    # CANNOT see (M2.1).
    #
    # GROUP_ASSEMBLE groups radios and checkboxes, because those are the kinds
    # HTML defines a grouping for. A health questionnaire rendered as pairs of
    # bare <button>s — the single most common shape in the insurance funnels
    # this product exists to crawl — is not radios, carries no name attribute,
    # and came out of pass 3 as N unrelated buttons. The walker could only
    # identify each question by its DOM ORDINAL, which is why the catalogue
    # called them "Question 1" … "Question 20": there was no other handle.
    #
    # The DOM did declare one, though, and nobody read it: the <fieldset> /
    # role=group / role=radiogroup container those buttons sit in. Keyed on
    # exactly that, so a question keeps its identity when a sibling question is
    # added above it, when the page re-renders, and across crawls.
    #
    # A SEPARATE FIELD, NOT AN OVERLOADED ``group_id``. Radio ``group_id`` hashes
    # key the remembered branch-walk overrides a previous crawl recorded, and
    # ``state_identity`` reads ``group_id`` as per-control identity when building
    # a page fingerprint — stamping it on buttons would re-key both. Consumers
    # that want "the question, whichever kind declared it" call
    # :func:`question_identity`.
    q_by_container: dict[tuple[str, str], list[int]] = {}
    for idx, rec in enumerate(records):
        if rec.get("group_id"):
            continue           # already ONE question via the choice grouping
        qk = _s(rec.get("question_key")).strip()
        if not qk:
            continue           # no declared container → nothing honest to group by
        q_by_container.setdefault(
            (rec.get("frame_selector") or "", qk), []).append(idx)

    questions_found = 0
    for (frame, qkey), indices in q_by_container.items():
        if len(indices) < 2:
            continue           # a lone control in a fieldset is just that control
        qgid = hashlib.sha256(
            f"q{frame}{qkey}".encode("utf-8")).hexdigest()[:32]
        answers = [records[i]["name"] for i in indices if records[i].get("name")]
        for i in indices:
            records[i]["question_group_id"] = qgid
            # Only the ANSWERS, and only when every member could be named: a
            # partial list presented as the answer set is the same fabrication
            # a clipped enumeration would be.
            if len(answers) == len(indices):
                records[i]["group_options"] = (
                    records[i].get("group_options") or answers)
                records[i]["group_size"] = len(indices)
        questions_found += 1

    # Pass 4: LOCATOR EVIDENCE (M2.2 / T-BR-03). Last, deliberately — it reads
    # the ordinal from pass 2 and the group identity from pass 3, so running it
    # earlier would silently emit locators with no scope and no question.
    verified_locators = attach_locators(records)

    logger.info(
        "qec.inventory.built controls=%d anchored=%d dangerous=%d "
        "radio_groups=%d radio_grouped=%d declared_questions=%d "
        "verified_locators=%d",
        len(records), anchored, sum(1 for r in records if r["danger"]),
        groups_found, grouped, questions_found, verified_locators,
    )
    return records

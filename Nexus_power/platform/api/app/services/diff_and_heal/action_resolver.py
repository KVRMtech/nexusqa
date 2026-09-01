"""Re-anchor resolver — broadens TrueFix beyond control-kind to the renamed /
moved control class.

Given the RECORDED intent for a control (its accessible label + kind) and a
snapshot of the LIVE page's accessibility tree captured at the moment the step
FAILED, find the control the test should now target. Deterministic, $0, no LLM.

Safety doctrine (same as the rest of TrueFix): it REFUSES (returns None) unless a
single live node is a confident, unambiguous match. No confident match → a
genuine rename can't be proven → the caller falls toward needs_review, and the
real-regression lane is never overridden. It can therefore never silently
re-bind to the wrong (semantically-different) control — the one unacceptable
error for a self-heal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The ARIA roles a live node could plausibly have for a given recorded control
# kind. An empty/unknown kind accepts any role (name-similarity then carries the
# decision). Keeps a re-anchor from binding a textbox onto a button, etc.
_ROLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "text": ("textbox", "searchbox", "combobox"),
    "select": ("combobox", "listbox", "menu"),
    "date": ("textbox", "combobox"),
    "button": ("button", "link"),
    "link": ("link", "button"),
    "toggle": ("checkbox", "radio", "switch"),
    "checkbox": ("checkbox", "switch"),
    "radio": ("radio",),
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _norm(s)))


def _similarity(a: str, b: str) -> float:
    """Cheap, dependency-free accessible-name similarity in [0,1]: token Jaccard,
    lifted ONLY for a genuine rename-by-qualifier (TOKEN containment, never a bare
    character substring).

    The lift recognises the common rename shape 'Travel Class' → 'Trip Travel Class'
    (the recorded label's TOKENS are wholly contained in the live name + extra
    qualifiers). It deliberately does NOT fire when the recorded label is a single
    generic token that merely appears inside a longer, semantically-different name —
    the old bare-substring test wrongly matched 'Class' → 'First Class Cabin Upgrade
    Class' (sim ≥0.85), steering a re-anchor onto the wrong control. We require:
      * full TOKEN containment of the smaller name in the larger (subset, not chars);
      * the smaller name is ≥ 2 tokens (one shared word like 'Class' can't lift); and
      * the overlap covers ≥ 50% of the LARGER name (so the recorded label is the
        bulk of the live name, not an incidental fragment of a long one).
    Otherwise plain token Jaccard carries the decision (still gated downstream by
    min_confidence + the ambiguity margin), so this only ever makes the matcher
    STRICTER — it can never re-bind to a control bare-substring would have refused."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    jac = len(inter) / len(ta | tb)
    if not inter:
        return jac  # 0.0 — no shared token, nothing to lift
    smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    contained = smaller <= larger                      # token subset, not char-substring
    cov_larger = len(inter) / len(larger)              # how much of the longer name is shared
    if contained and len(smaller) >= 2 and cov_larger >= 0.5:
        return max(jac, 0.85 + 0.15 * jac)
    return jac


@dataclass
class ReAnchor:
    name: str          # the live control's accessible name to re-bind to
    role: str          # its ARIA role (for the role rung of the ladder)
    confidence: float  # 0..1 name-similarity (RAW, pre state-bonus)
    rationale: str
    # The captured ARIA state of the chosen live node (subset of _ARIA_STATE_KEYS).
    # Empty when the node carried no state. Surfaced as an extra grounded signal for
    # diagnose; defaulted so existing constructors / callers stay byte-identical.
    state: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.state is None:
            self.state = {}


# The ARIA STATE keys Playwright's ``page.accessibility.snapshot()`` emits as
# top-level node properties (NOT nested under children). We copy them through
# verbatim so a re-anchor / diagnose can disambiguate same-name controls by their
# live state (an EXPANDED vs collapsed disclosure, a CHECKED vs unchecked toggle, a
# DISABLED vs enabled button, the SELECTED option, an option's heading LEVEL, an
# editable combobox). Booleans are kept as booleans; ``level`` stays an int. Absent
# keys are simply not copied — so a node with no state is byte-identical to today's
# {role, name}. Keeping this list explicit (vs copying every key) means we never
# leak a non-state property (e.g. ``children``) into the flattened node.
_ARIA_STATE_KEYS: tuple[str, ...] = (
    "expanded", "checked", "selected", "pressed", "disabled",
    "editable", "readonly", "required", "multiselectable", "level",
)


def flatten_aria(node, out: list | None = None) -> list[dict]:
    """Flatten a Playwright ``page.accessibility.snapshot()`` tree (nested
    {role,name,children,<state>}) into a flat list of named nodes.

    Each emitted node is ``{"role", "name", **state}`` where ``state`` is whatever
    subset of :data:`_ARIA_STATE_KEYS` was present on the source node
    (``expanded`` / ``checked`` / ``selected`` / ``pressed`` / ``disabled`` /
    ``editable`` / ``readonly`` / ``required`` / ``multiselectable`` / ``level``).

    BACKWARD-COMPATIBLE: callers that only read ``role`` + ``name`` are unaffected
    (the extra keys are ignored); a node that carries no state is identical to the
    pre-change ``{"role", "name"}`` shape, so no existing path changes behavior.
    """
    out = [] if out is None else out
    if isinstance(node, dict):
        role = node.get("role") or ""
        name = node.get("name") or ""
        if name:
            flat = {"role": role, "name": name}
            for k in _ARIA_STATE_KEYS:
                if k in node and node[k] is not None:
                    flat[k] = node[k]
            out.append(flat)
        for ch in (node.get("children") or []):
            flatten_aria(ch, out)
    elif isinstance(node, list):
        for ch in node:
            flatten_aria(ch, out)
    return out


def _state_match_bonus(recorded_state: dict | None, node: dict) -> float:
    """A small, BOUNDED disambiguation bonus in [0, ``_STATE_BONUS_MAX``] for how well
    a live node's captured ARIA state matches the recorded intent.

    Used ONLY as a tie-break between equally-named/scored candidates — it is added
    to the name-similarity score AFTER the role gate and is capped so it can never
    on its own lift a node past ``min_confidence`` (the floor is checked against the
    RAW name similarity, see ``resolve_reanchor``). So state can disambiguate two
    plausible renames, but never manufacture a confident match where the name alone
    wasn't already close — it only ever makes the matcher MORE precise.

    Matching is per-key and forgiving: a recorded key that the node doesn't expose
    contributes 0 (neither reward nor penalty), so a sparse capture never harms a
    correct match. ``recorded_state`` falsy / no comparable keys → 0.0."""
    if not recorded_state:
        return 0.0
    matched = 0
    compared = 0
    for k, want in recorded_state.items():
        if k not in node or node[k] is None:
            continue
        compared += 1
        got = node[k]
        if k == "level":
            if str(got) == str(want):
                matched += 1
        elif bool(got) == bool(want):
            matched += 1
    if compared == 0:
        return 0.0
    return _STATE_BONUS_MAX * (matched / compared)


# Bounded state tie-break. Sized JUST above the default ambiguity_margin (0.12) so a
# CLEAN state disambiguation (intent matches exactly one of two same-named candidates)
# can clear the ambiguity guard and resolve — but small enough that it only ever
# tie-breaks near-equal NAME scores, never reorders clearly-different ones. The
# min_confidence FLOOR is checked against RAW name similarity (pre-bonus), so this can
# never promote a weak-name node into a confident heal (see resolve_reanchor + tests).
_STATE_BONUS_MAX = 0.13


def resolve_reanchor(
    *,
    recorded_label: str,
    recorded_kind: str,
    live_nodes: list[dict],
    recorded_state: dict | None = None,
    min_confidence: float = 0.62,
    ambiguity_margin: float = 0.12,
) -> ReAnchor | None:
    """Find the live node most likely to be the recorded control under a new name.

    Returns a ``ReAnchor`` only when ONE node is a confident, unambiguous match:
      * its role is plausible for the recorded kind (when the kind is known),
      * its name similarity to the recorded label is ≥ ``min_confidence``,
      * and the runner-up is at least ``ambiguity_margin`` behind (so two equally
        plausible controls → refuse, not a coin-flip re-bind).
    A node whose name is IDENTICAL to the recorded label is skipped — the ladder
    would already have found it, so it isn't the renamed target.

    ``recorded_state`` (L4, optional): the recorded INTENT's ARIA state (e.g.
    ``{"checked": True}`` for a toggle the recording left on, ``{"expanded": True}``
    for a disclosure). When given, a small BOUNDED state-match bonus
    (``_state_match_bonus`` ≤ ``_STATE_BONUS_MAX`` < ``ambiguity_margin``) is added
    to each node's RANKING score so that, among equally-named candidates, the one
    whose live state matches the intent wins. CRUCIAL SAFETY: the bonus is applied
    ONLY to ranking + the ambiguity tie-break; the ``min_confidence`` floor is still
    checked against the RAW name similarity, so state can never on its own promote a
    weak-name node into a confident heal — it only ever makes the matcher STRICTER /
    more precise. Absent / unmatchable → the function is byte-identical to today.

    Returns ``None`` (refuse) otherwise — the caller must then fall toward
    needs_review and never auto-heal.
    """
    rl = _norm(recorded_label)
    if not rl or not live_nodes:
        return None
    cand_roles = _ROLE_CANDIDATES.get(_norm(recorded_kind), ())

    # (ranking_score, raw_name_sim, node). ranking_score = raw_sim + bounded state
    # bonus; raw_name_sim is what the min_confidence floor checks (state can't lift it).
    scored: list[tuple[float, float, dict]] = []
    for n in live_nodes:
        nm = n.get("name") or ""
        if not nm or _norm(nm) == rl:  # identical name wouldn't have broken
            continue
        role = (n.get("role") or "").lower()
        if cand_roles and role not in cand_roles:
            continue
        sim = _similarity(recorded_label, nm)
        if sim > 0:
            rank = sim + _state_match_bonus(recorded_state, n)
            scored.append((rank, sim, n))

    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    best_rank, best_sim, best = scored[0]
    # The min_confidence floor is checked against the RAW name similarity — the
    # state bonus can re-RANK candidates but can never admit a weak-name match.
    if best_sim < min_confidence:
        return None
    # Ambiguity guard. Two complementary checks so the state tie-break can ONLY ever
    # break the documented "two nodes, IDENTICAL accessible name, differ by state"
    # case — never close a gap between DIFFERENTLY-named (distinct) controls:
    #
    #   (a) RAW-name ambiguity: when the best and runner-up are within the margin on
    #       their RAW name similarity AND have DIFFERENT normalized names, they are two
    #       genuinely distinct controls and we refuse exactly as before — the state
    #       bonus is forbidden from rescuing this case. (When the close contenders share
    #       the same normalized name, this guard intentionally does not fire, so a clean
    #       state match may resolve the same-name tie.)
    #   (b) RANK ambiguity: the original guard on the ranking score (raw sim + bounded
    #       state bonus). A close runner-up on the FINAL ordering still refuses.
    if len(scored) > 1:
        runner_sim = scored[1][1]
        runner_name = scored[1][2].get("name") or ""
        if (best_sim - runner_sim) < ambiguity_margin and _norm(runner_name) != _norm(best["name"]):
            return None
        if (best_rank - scored[1][0]) < ambiguity_margin:
            return None

    best_state = {k: best[k] for k in _ARIA_STATE_KEYS if k in best and best[k] is not None}
    state_note = ""
    if recorded_state and _state_match_bonus(recorded_state, best) > 0:
        state_note = (
            " The live control's state "
            f"({', '.join(f'{k}={best_state[k]}' for k in best_state if k in recorded_state)}) "
            "also matches the recorded intent, which disambiguated it from a same-named control."
        )

    return ReAnchor(
        name=best["name"],
        role=(best.get("role") or ""),
        confidence=round(best_sim, 2),
        rationale=(
            f"The live page no longer has a control named '{recorded_label}', but "
            f"'{best['name']}' ({best.get('role') or 'control'}) is a close, "
            f"unambiguous match (similarity {round(best_sim, 2)}) — a likely "
            f"rename, not a removal." + state_note
        ),
        state=best_state,
    )


__all__ = [
    "ReAnchor", "flatten_aria", "resolve_reanchor",
    "recorded_state_intent", "_ARIA_STATE_KEYS",
]


def recorded_state_intent(observed: dict | None) -> dict:
    """Derive the recorded ARIA-STATE INTENT from a step's ``observed`` dict
    ({verb, label, kind, value, url}) for use as ``recorded_state`` in
    :func:`resolve_reanchor`. Deterministic, $0, grounded ONLY in what the recording
    actually shows — never invents a state.

    Mapping (high-precision; only fires on an unambiguous signal):
      * a toggle/checkbox/radio/switch verb or kind whose recorded value reads truthy
        ('on'/'true'/'checked'/'yes'/'1'/'selected') → ``{"checked": True}``; falsy
        ('off'/'false'/'unchecked'/'no'/'0') → ``{"checked": False}``.
      * an explicit 'expand'/'open'/'collapse'/'close' verb → ``{"expanded": ...}``.
    Anything else (a plain text fill, an unrecognised value) → ``{}`` so the
    resolver behaves exactly as before (no state tie-break)."""
    o = observed or {}
    verb = _norm(o.get("verb") or "")
    kind = _norm(o.get("kind") or "")
    val = _norm(o.get("value") or "")
    intent: dict = {}

    toggle_like = (
        verb in {"check", "uncheck", "toggle", "switch"}
        or kind in {"toggle", "checkbox", "radio", "switch"}
    )
    if toggle_like:
        if verb == "check" or val in _TRUTHY:
            intent["checked"] = True
        elif verb == "uncheck" or val in _FALSY:
            intent["checked"] = False

    if verb in {"expand", "open"}:
        intent["expanded"] = True
    elif verb in {"collapse", "close"}:
        intent["expanded"] = False

    return intent


_TRUTHY = {"on", "true", "checked", "yes", "1", "selected", "enabled"}
_FALSY = {"off", "false", "unchecked", "no", "0", "deselected", "disabled"}

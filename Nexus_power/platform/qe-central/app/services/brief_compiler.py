"""Plain-English brief → GROUNDED answer-key contract (ANSWERS P2, authoring-time).

Onboarding for 1000+ clients has to be tractable: a client types what they already
know — *"a 35-year-old non-smoker in TX quoting $500k over 20 years pays about
$75/mo; coverage can never exceed $2M"* — and this compiler turns it into the
structured ``{fill, outcomes, rules}`` contract the value/rule oracle consumes.

THE MOAT is not the LLM call — competitors have that.  It is the GROUNDING gate:
the LLM only PROPOSES; every proposed item is then RE-VALIDATED here against the
app's REAL captured field labels / value nodes.  An item we cannot tie to a real
label is NEVER silently activated — it is surfaced for human confirmation (design
DATA_ANSWERS §2, cross-cutting "grounded + provenance").  So a hallucinated field
can never enter an active contract.

Pure + dependency-free (no DB, no network, no SDK): the LLM is an INJECTED
``propose_fn`` seam, so the whole compiler is unit-testable with a fake proposer
and the real inference (platform-api ``LLMRouter``) is wired at the edge.
Deterministic at authoring time; there is NO LLM at run time (the compiled
contract is evaluated by the deterministic value/rule oracle).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _norm(label: str) -> str:
    """Normalize a label for tolerant matching: lowercase, strip non-alnum runs."""
    return re.sub(r"[^a-z0-9]+", " ", _s(label).lower()).strip()


def _tokens(label: str) -> frozenset[str]:
    return frozenset(t for t in _norm(label).split() if len(t) > 1)


def match_label(proposed: str, known_labels: Sequence[str]) -> str | None:
    """Return the REAL known label a proposed field maps to, or ``None``.

    Grounding precedence: exact (normalized) → token-subset → substring.  Returns
    the KNOWN label verbatim (so the contract uses the app's real accessible name,
    never the LLM's paraphrase).  ``None`` ⇒ ungrounded ⇒ needs human confirmation.
    """
    p = _norm(proposed)
    if not p:
        return None
    known = list(known_labels or ())
    norm_map = {_norm(k): k for k in known if _s(k).strip()}
    if p in norm_map:
        return norm_map[p]
    pt = _tokens(proposed)
    if pt:
        for k in known:
            kt = _tokens(k)
            if kt and (pt <= kt or kt <= pt):  # one is a token-subset of the other
                return k
    for k in known:  # substring fallback (either direction)
        nk = _norm(k)
        if nk and (p in nk or nk in p):
            return k
    return None


BRIEF_SYSTEM_INSTRUCTION = (
    "You compile a QA analyst's plain-English notes about an insurance/web app into a "
    "STRICT JSON contract. You NEVER invent field names, selectors, or values. You may "
    "ONLY use field labels from the provided KNOWN_LABELS list, verbatim. Output ONLY "
    "JSON, no prose."
)


def build_prompt(
    *, notes: str, answers: str,
    known_labels: Sequence[str], known_value_nodes: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Construct the grounded compile prompt.

    The prompt hard-constrains the model to the app's real vocabulary and asks it to
    put anything it cannot map into ``unmatched`` (so nothing is silently dropped or
    invented). The GROUNDING is re-checked in code regardless — the prompt is a hint,
    :func:`ground_and_assemble` is the enforcement.
    """
    labels = [l for l in (known_labels or []) if _s(l).strip()]
    nodes = [dict(n) for n in (known_value_nodes or []) if isinstance(n, Mapping)]
    node_hint = "\n".join(
        f"  - label {n.get('label')!r} → source_hint {n.get('source_hint') or n.get('selector')!r}"
        for n in nodes
    ) or "  (none captured — outcomes will need a human-provided source_hint)"
    return (
        f"{BRIEF_SYSTEM_INSTRUCTION}\n\n"
        f"KNOWN_LABELS (the ONLY field names you may use, verbatim):\n"
        + "\n".join(f"  - {l}" for l in labels) + "\n\n"
        f"KNOWN_VALUE_NODES (displayed outputs you may target for outcomes):\n{node_hint}\n\n"
        f"SEED NOTES (form inputs to fill):\n{_s(notes).strip() or '(none)'}\n\n"
        f"EXPECTED ANSWERS (outputs/invariants to verify):\n{_s(answers).strip() or '(none)'}\n\n"
        "Return JSON of this exact shape:\n"
        "{\n"
        '  "fill":     [{"label": "<KNOWN_LABEL>", "value": "<value>"}],\n'
        '  "outcomes": [{"field": "<name>", "expected": <number|string>, '
        '"tolerance": <number?>, "source_hint": "<selector from KNOWN_VALUE_NODES?>"}],\n'
        '  "rules":    [{"kind": "bound", "field": "<name>", "op": "<=|>=|<|>|==", '
        '"limit": <number>, "source_hint": "<selector?>"}],\n'
        '  "unmatched": [{"text": "<the phrase>", "reason": "<why it could not be mapped>"}]\n'
        "}\n"
    )


def parse_proposal(raw: Any) -> dict:
    """Tolerantly parse the LLM output into ``{fill, outcomes, rules, unmatched}``.

    Accepts a dict (already parsed) or a string (extracts the first JSON object,
    tolerating ```json fences / surrounding prose). NEVER raises — a malformed
    response yields empty lists (honest: nothing proposed) rather than a crash.
    """
    if isinstance(raw, Mapping):
        obj = dict(raw)
    else:
        text = _s(raw).strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
        try:
            obj = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            try:
                obj = json.loads(m.group(0)) if m else {}
            except Exception:
                obj = {}
    if not isinstance(obj, Mapping):
        obj = {}
    return {
        "fill": list(obj.get("fill") or []),
        "outcomes": list(obj.get("outcomes") or []),
        "rules": list(obj.get("rules") or []),
        "unmatched": list(obj.get("unmatched") or []),
    }


def ground_and_assemble(
    proposal: Mapping[str, Any],
    *, known_labels: Sequence[str], known_value_nodes: Sequence[Mapping[str, Any]] | None = None,
) -> dict:
    """RE-VALIDATE an (untrusted) LLM proposal against the app's real vocabulary and
    assemble the GROUNDED subset.  This is the safety gate — the LLM's own grounding
    claims are ignored; only what maps to a real known label / captured node here is
    marked grounded and placed in the active ``answer_key``.

    Returns ``{answer_key, review, grounded, ungrounded}``:
      * ``answer_key`` — ``{fill, outcomes, rules}`` of GROUNDED items only (safe to
        activate);
      * ``review`` — EVERY proposed item with ``grounded``/``needs_confirmation`` +
        a reason (the UI shows all; the human confirms/fixes the ungrounded ones).
    """
    known_labels = [l for l in (known_labels or []) if _s(l).strip()]
    node_hints = {
        _s(n.get("source_hint") or n.get("selector")).strip()
        for n in (known_value_nodes or []) if isinstance(n, Mapping)
    }
    node_hints.discard("")

    fill: dict[str, str] = {}
    outcomes: list[dict] = []
    rules: list[dict] = []
    review: list[dict] = []

    def _note(kind, field, grounded, reason, **extra):
        review.append({"kind": kind, "field": _s(field), "grounded": grounded,
                       "needs_confirmation": not grounded, "reason": reason, **extra})

    for item in proposal.get("fill") or []:
        if not isinstance(item, Mapping):
            continue
        proposed = _s(item.get("label") or item.get("field") or item.get("name"))
        value = _s(item.get("value"))
        real = match_label(proposed, known_labels)
        if real is not None:
            fill[real] = value
            _note("fill", real, True, f"matched known label '{real}'", value=value)
        else:
            _note("fill", proposed, False,
                  f"'{proposed}' does not match any captured field label", value=value)

    for item in proposal.get("outcomes") or []:
        if not isinstance(item, Mapping):
            continue
        field = _s(item.get("field") or item.get("name"))
        hint = _s(item.get("source_hint") or item.get("selector")).strip()
        real_label = match_label(field, known_labels)
        grounded = bool(hint and (not node_hints or hint in node_hints)) or real_label is not None
        rec = {"field": field or (real_label or ""), "expected": item.get("expected"),
               "tolerance": item.get("tolerance"), "source_hint": hint,
               "match": _s(item.get("match")) or None, "when": dict(item.get("when") or {})}
        if grounded:
            outcomes.append({k: v for k, v in rec.items() if v is not None})
            _note("outcome", rec["field"], True,
                  f"grounded via {'source_hint' if hint else 'known label'}", expected=rec["expected"])
        else:
            _note("outcome", field, False,
                  "no source_hint from a captured value node and field is not a known label — "
                  "a human must supply the selector for the displayed value", expected=rec["expected"])

    for item in proposal.get("rules") or []:
        if not isinstance(item, Mapping):
            continue
        field = _s(item.get("field") or item.get("name"))
        hint = _s(item.get("source_hint") or item.get("selector")).strip()
        real_label = match_label(field, known_labels)
        grounded = bool(hint and (not node_hints or hint in node_hints)) or real_label is not None
        rec = dict(item)
        rec["field"] = field or (real_label or "")
        if grounded:
            rules.append(rec)
            _note("rule", rec["field"], True, "grounded invariant", statement=_s(item.get("statement")))
        else:
            _note("rule", field, False,
                  "invariant target has no grounded node — needs a human-provided source_hint",
                  statement=_s(item.get("statement")))

    for um in proposal.get("unmatched") or []:
        if isinstance(um, Mapping):
            _note("unmatched", um.get("text", ""), False,
                  _s(um.get("reason")) or "the model could not map this to the app's vocabulary")

    grounded_n = sum(1 for r in review if r["grounded"])
    return {
        "answer_key": {"fill": fill, "outcomes": outcomes, "rules": rules},
        "review": review,
        "grounded": grounded_n,
        "ungrounded": len(review) - grounded_n,
    }


def compile_brief(
    *, notes: str, answers: str,
    known_labels: Sequence[str], known_value_nodes: Sequence[Mapping[str, Any]] | None = None,
    propose_fn: Callable[[str], Any] | None = None,
) -> dict:
    """Compile a plain-English brief into a grounded answer-key proposal.

    ``propose_fn(prompt) -> str|dict`` performs the LLM inference (injected — the
    real one calls the platform ``LLMRouter``; tests pass a fake).  When ``None`` or
    it fails, the compiler degrades HONESTLY: it proposes nothing and reports the
    reason, so onboarding falls back to manual authoring rather than a fabricated
    contract.  The result always carries the ``prompt`` for auditability.
    """
    prompt = build_prompt(notes=notes, answers=answers,
                          known_labels=known_labels, known_value_nodes=known_value_nodes)
    llm_error = ""
    raw: Any = {}
    if propose_fn is not None:
        try:
            raw = propose_fn(prompt)
        except Exception as exc:  # never let an LLM failure break onboarding
            llm_error = f"LLM proposal failed: {exc}"[:300]
    else:
        llm_error = "no LLM configured — manual authoring required"
    proposal = parse_proposal(raw)
    result = ground_and_assemble(
        proposal, known_labels=known_labels, known_value_nodes=known_value_nodes)
    result["prompt"] = prompt
    result["llm_error"] = llm_error
    return result


__all__ = ["compile_brief", "build_prompt", "parse_proposal", "ground_and_assemble", "match_label"]

"""P3 — Persona journey projector (the missing primitive, Δ3).

Generation mode is not a crawl: given ONE Master Catalog + the P1 trigger→child
rules + a persona's declared answers, this computes the journey that persona
would produce — which questions are executed, which are dynamically activated by
their answers, and which are skipped — analytically, without dispatching a walk.

This is what ``persona_diff`` could not do: that only diffs two ALREADY-crawled
journeys; this PREDICTS one from answers. A verifying crawl (dispatched with the
persona's answers as choice_overrides) then proves the predicted path — predicted
vs walked is the honesty check (G_INFERRED vs G_LIVE_CONFIRMED).

Pure + deterministic: plain dicts in, plain dict out. No DB, no I/O.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .catalog import question_id_for


def _norm(v: Any) -> str:
    return re.sub(r"[\s_\-]+", " ", str(v or "").strip().lower()).strip()


def rules_from_branches(
    branches: Sequence[Mapping[str, Any]],
    questions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Turn P1 branch rows (each carrying ``reveals``) into projector rules.

    The reconciler that closes the P1→P3 loop. A branch row is a trigger question
    (its ``control_signature``) answered with an ``option_label_norm`` that
    revealed some controls. This maps:
      * the TRIGGER to a catalog ``question_id`` via ``question_id_for`` on its
        signature — the same id the catalog gives that questionnaire question;
      * each REVEAL identity (``kind:name``) to a CHILD catalog ``question_id`` by
        matching its name against the catalog.

    Reveals that match no named catalog question (e.g. a bare ``button:yes`` that
    carries no distinct question identity) are dropped — honestly, not faked. A
    rule is emitted only when the trigger and at least one child both resolve.

    Returns ``[{question_id, option, reveals_question_ids}]`` — the exact shape
    ``project_traversal`` consumes.
    """
    by_name: dict[str, str] = {}
    for q in questions:
        if isinstance(q, Mapping):
            qid = str(q.get("question_id") or "")
            if qid:
                by_name.setdefault(_norm(q.get("name")), qid)

    rules: list[dict[str, Any]] = []
    for b in branches:
        if not isinstance(b, Mapping):
            continue
        reveals = b.get("reveals")
        if not isinstance(reveals, (list, tuple)) or not reveals:
            continue
        sig = str(b.get("control_signature") or "")
        label = str(b.get("control_label_norm") or b.get("control_label") or "")
        if not sig and not label:
            continue
        trigger_qid = question_id_for({"signature": sig, "name": label})
        option = str(b.get("option_label_norm") or b.get("option") or "")
        kids: list[str] = []
        seen: set[str] = set()
        for r in reveals:
            name = str(r).split(":", 1)[-1]        # "kind:name" → name
            kid = by_name.get(_norm(name))
            if kid and kid != trigger_qid and kid not in seen:
                seen.add(kid)
                kids.append(kid)
        if trigger_qid and kids:
            rules.append({
                "question_id": trigger_qid,
                "option": option,
                "reveals_question_ids": kids,
            })
    return rules


def project_traversal(
    questions: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one persona's journey over the catalog + trigger→child rules.

    ``questions``: the Master Catalog rows — each ``{question_id, ...}``.
    ``rules``: trigger→child rules — ``{question_id, option, reveals_question_ids}``
        (the P1 reveals resolved to child question ids). Answering ``question_id``
        with ``option`` makes those children visible.
    ``answers``: ``{question_id: chosen_option}`` — the persona's declared answers.

    Returns ``{visible, executed, activated, skipped}`` (each a list of question_ids
    in catalog order):
      * ``visible``   — questions on this persona's path (base + activated);
      * ``executed``  — visible questions the persona actually answered;
      * ``activated`` — questions made visible by an answer (i.e. children);
      * ``skipped``   — catalog questions the persona never reaches.
    """
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for q in questions:
        if not isinstance(q, Mapping):
            continue
        qid = str(q.get("question_id") or "")
        if qid and qid not in seen:
            seen.add(qid)
            ordered_ids.append(qid)
    all_set = set(ordered_ids)

    # Resolve the rules; a child is any question revealed by some option.
    reveal_map: dict[tuple[str, str], list[str]] = {}
    child: set[str] = set()
    for r in rules:
        if not isinstance(r, Mapping):
            continue
        qid = str(r.get("question_id") or "")
        opt = _norm(r.get("option"))
        kids = [
            str(k) for k in (r.get("reveals_question_ids") or [])
            if str(k) in all_set]
        if not qid:
            continue
        reveal_map[(qid, opt)] = kids
        child.update(kids)

    # Base questions are always visible; children only once their trigger fires.
    visible: set[str] = {q for q in ordered_ids if q not in child}
    # Answer lookup, normalized.
    ans = {str(k): _norm(v) for k, v in answers.items()}

    changed = True
    while changed:                       # fixpoint: an activated child can trigger more
        changed = False
        for qid in list(visible):
            a = ans.get(qid)
            if a is None:
                continue
            for kid in reveal_map.get((qid, a), ()):
                if kid not in visible:
                    visible.add(kid)
                    changed = True

    executed = [q for q in ordered_ids if q in visible and ans.get(q) is not None]
    activated = [q for q in ordered_ids if q in visible and q in child]
    skipped = [q for q in ordered_ids if q not in visible]
    return {
        "visible": [q for q in ordered_ids if q in visible],
        "executed": executed,
        "activated": activated,
        "skipped": skipped,
    }


def persona_answers_for(
    questions: Sequence[Mapping[str, Any]],
    expected_values: Mapping[str, Any],
) -> dict[str, str]:
    """Map a persona's declared answer sheet (``value_key → value``) onto catalog
    question ids, so the projector can run it.

    A persona owns expected values keyed by field name/semantic key; a catalog
    question carries ``name`` and ``semantic_type``. This resolves an answer to a
    ``question_id`` by matching on normalized name or semantic type. Unmatched
    persona values are ignored (the persona simply doesn't answer that question);
    a question with no persona answer is left unanswered — never guessed.
    """
    by_name: dict[str, str] = {}
    by_sem: dict[str, str] = {}
    for q in questions:
        if not isinstance(q, Mapping):
            continue
        qid = str(q.get("question_id") or "")
        if not qid:
            continue
        by_name[_norm(q.get("name"))] = qid
        sem = _norm(q.get("semantic_type"))
        if sem:
            by_sem.setdefault(sem, qid)

    out: dict[str, str] = {}
    for key, val in expected_values.items():
        nk = _norm(key)
        qid = by_name.get(nk) or by_sem.get(nk)
        if qid:
            out[qid] = str(val)
    return out

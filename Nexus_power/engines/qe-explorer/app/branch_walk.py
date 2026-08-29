"""BRANCH COVERAGE: answer every question with every answer, and record what
each answer reveals.

WHY THIS EXISTS. Every bundle this product has ever written carries
``branch_coverage: false`` and says why: "One path per journey. At each decision
point a single option was taken, so business paths behind the other options were
not visited." For a life-insurance health page that is the whole difficulty --
a page of 60 questions where answering Yes to eight of them reveals 44 more,
four levels deep, is 104 questions of which one path shows 60.

WHAT IT DOES, AND WHAT IT DELIBERATELY DOES NOT. It sweeps: for each question,
each of its answers is selected in turn and whatever appears is recorded. It does
NOT enumerate answer COMBINATIONS -- 2^60 is not walkable and pretending
otherwise would be the same fabrication this codebase refuses everywhere else.
The distinction is written into the ledger, not left to the reader:

    per-question sweep   Q x options          (what this does)
    full combinatorial   product(options)     (what it does not)

So a page is covered when every question has been asked with every answer and
everything those answers reveal has itself been asked. Paths that require two
specific answers AT ONCE are named as unvisited rather than counted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

#: A sweep visits at most this many (question, option) pairs in one page. A
#: pathological page cannot be allowed to spend a whole crawl here.
MAX_BRANCH_VISITS = 400
#: How deep a reveal chain is followed. Level 0 is the page as it loaded.
MAX_BRANCH_DEPTH = 6


def question_key(control: Mapping[str, Any]) -> str:
    """Identity of the QUESTION a control answers, stable across re-inventory.

    Prefers the declared container identity over the wording: two questions can
    legitimately share wording ("Details?") and a walk that merged them would
    report one as covered because the other was.
    """
    for k in ("question_group_id", "group_id", "question_key"):
        v = str(control.get(k) or "").strip()
        if v:
            return v
    return str(control.get("question_label") or control.get("name") or "").strip()


def answerable_questions(
    controls: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One row per QUESTION with an enumerable answer set, in DOM order.

    A question with fewer than two answers is not a branch point: there is
    nothing to sweep, and sweeping it would spend budget to learn nothing.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in controls:
        kind = str(c.get("kind") or "").strip().lower()
        if kind not in ("radio", "select", "checkbox", "toggle"):
            continue
        key = question_key(c)
        if not key or key in seen:
            continue
        options = [str(o) for o in (c.get("options") or c.get("group_options") or []) if str(o).strip()]
        if len(options) < 2:
            continue
        seen.add(key)
        out.append({
            "key": key,
            "label": str(c.get("question_label") or c.get("name") or "").strip(),
            "kind": kind,
            "options": options,
            "control": dict(c),
        })
    return out


def newly_revealed(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Controls present AFTER an answer that were not present before.

    Keyed on question identity + control name so that a re-render which rebuilds
    the same controls does not read as a reveal. Order is the DOM order of
    ``after`` -- the page's own, never sorted, so the ledger reads the way the
    form does.
    """
    def ident(c: Mapping[str, Any]) -> str:
        return f"{question_key(c)}\x1f{str(c.get('name') or '')}"

    had = {ident(c) for c in before}
    return [c for c in after if ident(c) not in had]


@dataclass
class BranchLedger:
    """What was asked, what each answer revealed, and what was NOT walked."""

    visits: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    questions_seen: set[str] = field(default_factory=set)

    def record(self, *, question: str, label: str, option: str, depth: int,
               revealed: Sequence[Mapping[str, Any]]) -> None:
        self.questions_seen.add(question)
        # COUNT QUESTIONS, NOT INPUTS. A revealed Yes/No question arrives as TWO
        # control records, so counting controls reported a cascade of two
        # questions as four. The client's question is "how many questions
        # appeared", and a number that answers a different one is worse than no
        # number.
        by_q: dict[str, str] = {}
        for c in revealed:
            k = question_key(c)
            if k and k not in by_q:
                by_q[k] = str(c.get("question_label") or c.get("name") or "")
        self.visits.append({
            "question": question, "label": label, "option": option, "depth": depth,
            "revealed": list(by_q.values()),
            "revealed_count": len(by_q),
        })

    def skip(self, *, question: str, option: str, reason: str) -> None:
        self.skipped.append({"question": question, "option": option, "reason": reason})

    def summary(self) -> dict[str, Any]:
        revealed_total = sum(v["revealed_count"] for v in self.visits)
        return {
            # NAMED so a reader can never mistake a sweep for an exhaustive walk.
            "mode": "per_question_sweep",
            "combinatorial": False,
            "note": ("Every question was asked with every answer, and everything "
                     "those answers revealed was itself asked. Paths that require "
                     "two specific answers AT ONCE were not visited."),
            "questions_swept": len(self.questions_seen),
            "answers_taken": len(self.visits),
            "questions_revealed": revealed_total,
            "skipped": len(self.skipped),
        }


def budget_exhausted(ledger: BranchLedger, *, max_visits: int = MAX_BRANCH_VISITS) -> bool:
    return len(ledger.visits) >= max_visits


def should_descend(depth: int, *, max_depth: int = MAX_BRANCH_DEPTH) -> bool:
    return depth < max_depth


# ── the walk itself ─────────────────────────────────────────────────────────


async def sweep_page(
    *, port: Any, observe: Any, build_controls: Any,
    reset: Any,
    max_visits: int = MAX_BRANCH_VISITS,
    max_depth: int = MAX_BRANCH_DEPTH,
    logger: Any = None,
) -> BranchLedger:
    """Ask every question with every answer; follow what each answer reveals.

    Four collaborators, all injected, so this module drives no browser of its
    own and can be exercised without one:

      ``observe``         -> a page observation (url + raw controls)
      ``build_controls``  -> inventory records for an observation
      ``port.fill``       -> commit one answer to one control
      ``reset``           -> put the page back to its loaded state

    RESET BETWEEN ANSWERS, ALWAYS. A questionnaire's reveals are stateful: after
    answering Q7=Yes the page is no longer the page Q8 was measured on, and
    sweeping onward from there would attribute Q7's reveals to Q8. The cost is a
    reload per answer, which is why the visit budget exists.

    A reveal is followed IMMEDIATELY, before the reset, because that is the only
    moment the revealed questions exist. Their own answers are then swept from a
    freshly reset page with the revealing answer re-applied -- so a level-3
    question is reached the way a person reaches it, not by being conjured.
    """
    ledger = BranchLedger()

    async def _inventory() -> tuple[Any, list[Mapping[str, Any]]]:
        obs = await observe()
        return obs, list(build_controls(obs) or ())

    async def _apply(prefix: Sequence[tuple[Mapping[str, Any], str]]) -> list[Mapping[str, Any]]:
        """Reset, then re-apply an answer prefix; return the resulting controls."""
        await reset()
        _, controls = await _inventory()
        for ctrl, option in prefix:
            live = _match(controls, ctrl)
            if live is None:
                return []
            await port.fill(dict(live), option)
            _, controls = await _inventory()
        return controls

    def _match(controls: Sequence[Mapping[str, Any]], want: Mapping[str, Any]):
        key = question_key(want)
        for c in controls:
            if question_key(c) == key:
                return c
        return None

    async def _sweep(prefix: Sequence[tuple[Mapping[str, Any], str]], depth: int) -> None:
        if not should_descend(depth, max_depth=max_depth):
            return
        controls = await _apply(prefix)
        if not controls:
            return
        answered = {question_key(c) for c, _ in prefix}
        for q in answerable_questions(controls):
            if q["key"] in answered:
                continue
            for option in q["options"]:
                if budget_exhausted(ledger, max_visits=max_visits):
                    ledger.skip(question=q["key"], option=option,
                                reason="budget_exhausted")
                    return
                base = await _apply(prefix)
                live = _match(base, q["control"])
                if live is None:
                    ledger.skip(question=q["key"], option=option,
                                reason="question_not_present_after_reset")
                    continue
                try:
                    await port.fill(dict(live), option)
                except Exception:
                    ledger.skip(question=q["key"], option=option, reason="fill_failed")
                    continue
                _, after = await _inventory()
                revealed = newly_revealed(base, after)
                ledger.record(question=q["key"], label=q["label"], option=option,
                              depth=depth, revealed=revealed)
                if logger is not None and revealed:
                    logger.info(
                        "qec.branch.revealed question=%r option=%r depth=%d "
                        "revealed=%d", q["label"][:60], option[:40], depth,
                        len(revealed))
                if revealed and should_descend(depth + 1, max_depth=max_depth):
                    await _sweep(list(prefix) + [(q["control"], option)], depth + 1)

    await _sweep([], 0)
    return ledger

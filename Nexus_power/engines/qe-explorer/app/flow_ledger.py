"""BUSINESS FLOWS — a journey is a PATH, and a path has to be able to say whether
it finished.

The crawl records states and actions. A business flow is neither: it is a *path*
through them — an entry, an ordered sequence of steps, and a terminal. Without
flows as objects the product can count pages and fields but cannot answer the only
question a client actually asks: **did you get all the way through Apply?**

The distinction this module exists to keep is between:

    COMPLETED   the walk reached a natural end — a submit boundary, or a step with
                nothing left to advance. The journey was covered.
    TRUNCATED   the walk stopped because it ran out of budget, looped, or was
                cancelled. The journey was NOT covered, and saying it was is the
                same class of claim as a green test that never ran.

Six steps of a fifteen-step funnel is not "the Apply flow". A crawl that reports
it as one has green-washed coverage, which is the failure this whole product
exists to prevent — so the terminal reason is recorded on every flow and the
completion flag is derived from it, never set directly.

Pure + deterministic: no clock, no I/O.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "TERMINAL_SUBMIT_BOUNDARY", "TERMINAL_NO_ADVANCE", "TERMINAL_BUDGET",
    "TERMINAL_LOOP", "TERMINAL_CANCELLED", "TERMINAL_ORACLE_UNAVAILABLE",
    "COMPLETING_TERMINALS", "flow_id_for", "build_flow", "summarize",
    "activated_signatures",
]


def activated_signatures(
    before: Iterable[Mapping[str, Any]],
    after: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Value-free identities of controls ``after`` an answer that were not there
    (or not as many) ``before`` it.

    This is the trigger→child signal (Journey Graph P1): answering an option can
    REVEAL follow-up questions (a "Yes" to a health question shows its detail
    block). Diffing the inventory just before an answer against the inventory just
    after it names exactly what that answer activated — no page-fork, using the
    re-observe the walk already performs.

    Multiplicity matters: revealing ANOTHER "Yes/No" question adds controls whose
    accessible name already existed, so a set-diff would miss it. We compare COUNTS
    and report any identity whose count rose — a new field, or one more instance of
    an existing label. Identity is ``kind:accessible-name`` — UI shape, like every
    other label in the graph; no user value ever enters it.
    """
    def _key(c: Mapping[str, Any]) -> str:
        name = str(c.get("name") or "").strip().lower()
        kind = str(c.get("kind") or "").strip().lower()
        return ("%s:%s" % (kind, name))[:80] if name else ""

    b = Counter(_key(c) for c in before if isinstance(c, Mapping))
    b.pop("", None)
    a = Counter(_key(c) for c in after if isinstance(c, Mapping))
    a.pop("", None)
    revealed = [k for k in a if a[k] > b.get(k, 0)]   # Counter preserves order
    return revealed[:64]

#: The walk stopped at a control it may not cross without an approval — the end of
#: the journey as far as a non-mutating crawl is concerned. The funnel WAS covered.
TERMINAL_SUBMIT_BOUNDARY = "submit_boundary"
#: Nothing left to advance: the last step offered no Next/Continue. Also complete.
TERMINAL_NO_ADVANCE = "no_advance"
#: Ran out of step / advance / depth budget. NOT complete — there was more funnel.
TERMINAL_BUDGET = "budget_exhausted"
#: The next step was a state already seen — the app looped us. NOT complete.
TERMINAL_LOOP = "loop"
#: The crawl was stopped. NOT complete.
TERMINAL_CANCELLED = "cancelled"
#: The regex tiers found nothing to advance AND the agent oracle could not be
#: reached (outage, timeout, cap, open circuit, unreadable reply). Whether more
#: funnel existed is UNKNOWN — and unknown is NOT complete. This terminal is a
#: PLATFORM failure, never the tenant application's fault: the honest remedy is
#: "re-crawl when the advance service is healthy", not a defect report.
TERMINAL_ORACLE_UNAVAILABLE = "oracle_unavailable"

#: Exactly the terminals that mean the journey was actually covered. A terminal not
#: in this set produces ``completed=False``, and there is deliberately no way for a
#: caller to override that.
COMPLETING_TERMINALS = frozenset({TERMINAL_SUBMIT_BOUNDARY, TERMINAL_NO_ADVANCE})

#: The app REFUSED to move on: the page did not change at all.
#:
#: ``no_advance`` is deliberately NOT here. It means "nothing on this page could
#: advance it", which at the END of a funnel is correct completion, not a
#: contradiction — a review page that displays a premium and offers no next step
#: is exactly what success looks like. Including it fired the tripwire on the
#: very journey it was built to protect, and a tripwire that cries wolf on
#: success is one nobody reads.
_ADVANCE_REFUSED_TERMINALS = frozenset({TERMINAL_LOOP})


def flow_id_for(entry_fingerprint: str) -> str:
    """A flow is identified by where it STARTS.

    The entry state is the only part of a journey that is stable across crawls: the
    steps change as the app changes, and the terminal changes as budgets change. So
    the same funnel keeps one id and a client can watch it get deeper over time
    rather than seeing a new flow appear every crawl."""
    return hashlib.sha256(f"flow::{entry_fingerprint}".encode("utf-8")).hexdigest()[:24]


def build_flow(*, entry_fingerprint: str, entry_url: str, entry_title: str,
               steps: Sequence[Mapping[str, Any]], terminal: str,
               terminal_url: str = "", outcome_values: Iterable[Mapping[str, Any]] = (),
               max_steps: int = 0) -> dict[str, Any]:
    """One recorded journey.

    ``completed`` is DERIVED from the terminal reason, never passed in — the whole
    point is that a truncated walk cannot be reported as a covered journey."""
    term = str(terminal or TERMINAL_BUDGET)
    step_list: list[dict[str, Any]] = []
    for s in (steps or ()):
        entry: dict[str, Any] = {
            "fingerprint": str(s.get("fingerprint") or ""),
            "url": str(s.get("url") or ""),
            "title": str(s.get("title") or "")[:200],
            "fields_filled": int(s.get("fields_filled") or 0),
            "fields_unfilled": int(s.get("fields_unfilled") or 0),
        }
        # WHO decided the advance out of this step (absent on the terminal
        # step and on pre-evidence manifests): tier 1/2 = deterministic regex,
        # tier 3 = the agent oracle. ``signature`` is the oracle's value-free
        # decision-point signature — the key a PROVEN pick is remembered under.
        adv = s.get("advance")
        if isinstance(adv, Mapping):
            advance: dict[str, Any] = {
                "tier": int(adv.get("tier") or 0),
                "control_name": str(adv.get("control_name") or "")[:120],
                "oracle": bool(adv.get("oracle")),
            }
            sig = str(adv.get("signature") or "")
            if sig:
                advance["signature"] = sig[:64]
            entry["advance"] = advance
        # DECISION POINTS (Journey Graph C0): the forks this step offered —
        # each with its enumerated options, the option the fill took (absent
        # when the field went unanswered: discovered, not decided), and the
        # provenance of that choice. Value-free: labels are product UI text.
        dps = s.get("decision_points")
        if isinstance(dps, Sequence) and not isinstance(dps, (str, bytes)):
            cleaned: list[dict[str, Any]] = []
            for dp in list(dps)[:24]:
                if not isinstance(dp, Mapping):
                    continue
                rec: dict[str, Any] = {
                    "control_signature": str(dp.get("control_signature") or "")[:64],
                    "control_label": str(dp.get("control_label") or "")[:120],
                    "options": [str(o)[:80] for o in (dp.get("options") or ())][:24],
                    "provenance": str(dp.get("provenance") or "")[:40],
                }
                # The QUESTION a radio group's members all share — value-free
                # (a hash of the DOM's own grouping declaration). Without it the
                # fold cannot tell four members of one choice apart from four
                # independent choices.
                group_id = str(dp.get("group_id") or "")[:64]
                if group_id:
                    rec["group_id"] = group_id
                choice = dp.get("choice")
                if choice:
                    rec["choice"] = str(choice)[:80]
                # A next-action fork classifies each option (forward / destructive
                # / navigational) so the fold knows which branch is walkable and
                # which is surfaced-but-blocked. Value-free (labels + class names).
                oc = dp.get("option_classes")
                if isinstance(oc, Mapping):
                    rec["option_classes"] = {
                        str(k)[:80]: str(v)[:20] for k, v in list(oc.items())[:24]}
                # What walking the taken option ACTIVATED (trigger→child, P1) —
                # value-free control identities diffed from the re-observe. Present
                # only when a discovery walk recorded it; the fold stores it on the
                # walked branch so "Yes reveals these, No does not" becomes a rule.
                rv = dp.get("reveals")
                if isinstance(rv, Sequence) and not isinstance(rv, (str, bytes)):
                    revs = [str(x)[:80] for x in list(rv)[:64] if x]
                    if revs:
                        rec["reveals"] = revs
                cleaned.append(rec)
            if cleaned:
                entry["decision_points"] = cleaned
        step_list.append(entry)
    # An outcome the funnel produced — a premium, an eligibility decision. This is
    # what makes a flow worth having walked, and (in the next phase) what decides
    # whether a different choice produced a genuinely different business path.
    # The producer of these nodes is ``_displayed_values``, whose contract is
    # {label, selector, TEXT} — the rendered value lives in ``text``. Reading only
    # ``value`` silently emptied every outcome: the crawl captured "Estimated
    # Monthly Premium = $9.26" on the page and the journey then recorded a
    # premium of "". A journey without its outcome is a walk, not evidence —
    # proving the funnel runs is worth far less than proving what it produced.
    outcomes = [
        {"label": str(v.get("label") or "")[:120],
         "value": str(v.get("value") or v.get("text") or "")[:200],
         "value_type": str(v.get("value_type") or "")}
        for v in (outcome_values or ())
    ][:12]

    unfilled = sum(s["fields_unfilled"] for s in step_list)
    # ── SELF-CONTRADICTION TRIPWIRE ───────────────────────────────────────────
    # We claim to have filled N fields, and then the app refused to move. Those
    # two statements cannot both be comfortable. Either the app is genuinely
    # blocked (a real finding worth surfacing) or — far more often — a fill was
    # recorded as successful while the control never took the value, and the
    # crawl went on to click a Continue that was still disabled.
    #
    # This is the cheapest possible check on our own honesty: no LLM, no app
    # knowledge, true for every application. It exists because a whole class of
    # defects hid behind "filled" for an entire debugging session — a locator
    # matching nothing, a read-back in the wrong vocabulary, a placeholder
    # accepted as an answer. Each of those was silent; every one would have
    # raised THIS flag on the first crawl of the first app.
    #
    # It deliberately does NOT decide who lied. It records the contradiction and
    # names the steps, so one crawl is a lead and a fleet-wide count is a
    # systemic bug found once instead of a thousand times.
    filled_total = sum(s["fields_filled"] for s in step_list)
    contradicted = bool(filled_total) and term in _ADVANCE_REFUSED_TERMINALS
    return {
        "flow_id": flow_id_for(entry_fingerprint),
        "entry_fingerprint": entry_fingerprint,
        "entry_url": entry_url,
        "entry_title": str(entry_title or "")[:200],
        "steps": step_list,
        "step_count": len(step_list),
        "terminal": term,
        "terminal_url": terminal_url,
        "completed": term in COMPLETING_TERMINALS,
        # A journey walked to its end while fields went unanswered was covered
        # STRUCTURALLY but not with real data. Both facts matter and neither
        # substitutes for the other.
        "fields_unanswered": unfilled,
        "fully_answered": unfilled == 0,
        "outcome_values": outcomes,
        # Recorded so a truncated flow can say what it hit rather than only that it
        # stopped — "6 of a 6-step budget" is actionable; "stopped" is not.
        "step_budget": int(max_steps or 0),
        #: TRIPWIRE: this flow filled fields and STILL could not advance. Either
        #: the app is blocked (a finding) or a fill lied (a defect). Never a
        #: healthy outcome, and never silent.
        "advance_contradicts_fills": contradicted,
        "fields_filled_total": filled_total,
    }


def summarize(flows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The coverage claim, stated so it cannot be read as more than it is.

    ``branch_coverage`` is reported as ``False`` explicitly rather than omitted: a
    reader must not be able to infer that walking every flow once means every
    business path was covered. One path per funnel is one path, and the decision
    points that fork the funnel have not been explored."""
    total = len(flows)
    done = [f for f in flows if f.get("completed")]
    answered = [f for f in done if f.get("fully_answered")]
    truncated = [f for f in flows if not f.get("completed")]
    reasons: dict[str, int] = {}
    for f in truncated:
        r = str(f.get("terminal") or "")
        reasons[r] = reasons.get(r, 0) + 1
    # WHO advanced the walks — the audit rollup. Tier 3 counts are the crawls'
    # agent decisions (each one attributable to a specific step's evidence),
    # and the telemetry that grows the deterministic vocabulary over time.
    advances_by_tier: dict[str, int] = {}
    oracle_advances = 0
    total_intent_unmet = 0
    for f in flows:
        for s in f.get("steps") or ():
            if not isinstance(s, Mapping):
                continue
            total_intent_unmet += int(s.get("intent_unmet") or 0)
            adv = s.get("advance")
            if not isinstance(adv, Mapping):
                continue
            tier = str(int(adv.get("tier") or 0))
            advances_by_tier[tier] = advances_by_tier.get(tier, 0) + 1
            if adv.get("oracle"):
                oracle_advances += 1
    return {
        "flows_found": total,
        "flows_completed": len(done),
        "flows_fully_answered": len(answered),
        "flows_truncated": len(truncated),
        "truncation_reasons": reasons,
        "advances_by_tier": advances_by_tier,
        "oracle_advances": oracle_advances,
        "intent_unmet": total_intent_unmet,
        # SELF-CONTRADICTION ROLLUP. How many journeys claimed to fill fields and
        # were then refused an advance. On ONE crawl this is a lead; across a
        # fleet it is how a systemic fill defect is found ONCE rather than
        # rediscovered per application. Non-zero always deserves a look — it
        # means the crawl and the application disagree about whether a form was
        # completed, and only one of them can be right.
        "advance_contradicts_fills": sum(
            1 for f in flows if f.get("advance_contradicts_fills")),
        "deepest_flow_steps": max((int(f.get("step_count") or 0) for f in flows), default=0),
        # THE HONESTY FLAG. One path per funnel was walked. Which option was taken
        # at each decision point was decided once, and the alternatives — a
        # different premium, a different eligibility outcome — were never visited.
        "branch_coverage": False,
        "branch_coverage_note": (
            "One path per journey. At each decision point a single option was taken, "
            "so business paths behind the other options were not visited."
        ),
    }

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
    "TERMINAL_SUBMIT_BOUNDARY", "TERMINAL_SUBMIT_CROSSED", "TERMINAL_NO_ADVANCE",
    "TERMINAL_BUDGET",
    "TERMINAL_LOOP", "TERMINAL_CANCELLED", "TERMINAL_ORACLE_UNAVAILABLE",
    "TERMINAL_CONFIRMATION", "resolve_walk_terminal",
    "COMPLETING_TERMINALS", "flow_id_for", "build_flow", "summarize",
    "activated_signatures", "journeys_completed",
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

#: A4.3 — the walk CROSSED an approved irreversible boundary and landed. Distinct
#: from ``submit_boundary``, which means it stopped in front of one. Both are
#: complete COVERAGE of the funnel; only this one can carry a journey outcome.
TERMINAL_SUBMIT_CROSSED = "submit_crossed"

#: M1.4 — THE APPLICATION ITSELF SAID THE JOURNEY WAS DONE.
#:
#: A step landed on a page that DECLARED success: a ``role=status`` region it
#: published, success-shaped text that appeared as a result of the click, or a
#: non-error confirmation dialog it opened (see
#: :func:`app.boundary.is_confirmation_landing` — that predicate, and nothing
#: about a URL, a button label or a page title, is what "recognized" means here).
#:
#: This is a COMPLETING terminal, and it deliberately OUTRANKS ``loop``. A
#: confirmation page is a page with an application number on it and a handful of
#: ways to leave — "Back to Dashboard", "Print Confirmation", "New Application".
#: Every one of those is clickable, none of them continues the funnel, and the
#: walk that clicked one and found itself somewhere it had already been recorded
#: the whole journey as ``loop`` / ``completed=false``. The funnel was walked to
#: its business end; the ledger simply had no word for that end.
TERMINAL_CONFIRMATION = "confirmation"

#: Exactly the terminals that mean the journey was actually covered. A terminal not
#: in this set produces ``completed=False``, and there is deliberately no way for a
#: caller to override that.
COMPLETING_TERMINALS = frozenset({TERMINAL_SUBMIT_BOUNDARY, TERMINAL_NO_ADVANCE,
                                  TERMINAL_SUBMIT_CROSSED, TERMINAL_CONFIRMATION})

#: The three verdicts a caller may pass to :func:`resolve_walk_terminal` as its
#: "there was nothing left to click" answer. Whitelisted rather than accepted
#: verbatim so the precedence function can never be used as a back door for a
#: caller-chosen terminal — which is the same reason ``completed`` is derived
#: rather than passed in.
_NOTHING_TO_CLICK_TERMINALS = frozenset({TERMINAL_SUBMIT_BOUNDARY,
                                         TERMINAL_NO_ADVANCE,
                                         TERMINAL_ORACLE_UNAVAILABLE})


def resolve_walk_terminal(*, cancelled: bool = False, confirmation: bool = False,
                          nothing_to_click: str = "",
                          budget_left: bool = True) -> str:
    """WHY A WALK ENDED, decided once, in one place, in a fixed order (T-CF-03).

    The walk can end for several reasons AT THE SAME TIME — a confirmation page
    that also still offers a clickable control, with the step budget nearly
    spent. Which of them gets recorded decides whether the journey reads as
    covered, so the order may not be an accident of how the ``if`` chain was
    typed. It is:

      1. ``cancelled``   an operator stopped the crawl. Nothing OBSERVED may
                         override an explicit human abort, or "I stopped it"
                         would silently become "it finished".
      2. ``confirmation`` the application declared success. This is the most
                         specific statement available about why a walk ended —
                         not "we ran out of things to click" but "the app told
                         us the transaction is done" — so it outranks every
                         inference below it.
      3. ``nothing_to_click`` the caller's own three-way verdict:
                         ``submit_boundary`` (stopped in front of a commit
                         control), ``no_advance`` (nothing on the page advances)
                         or ``oracle_unavailable`` (we could not find out).
      4. ``budget``      the walk was TRUNCATED. Not complete, and it must not
                         be able to hide behind a weaker reason below it.
      5. ``loop``        the click produced nothing, or landed somewhere this
                         journey had already been.

    Nothing here weakens loop detection: ``loop`` is still the answer for every
    walk that did not observe a confirmation, and ``confirmation`` is not a flag
    a caller may assert — it is the output of
    :func:`app.boundary.is_confirmation_landing` over what the browser observed.
    """
    if cancelled:
        return TERMINAL_CANCELLED
    if confirmation:
        return TERMINAL_CONFIRMATION
    if nothing_to_click:
        if nothing_to_click not in _NOTHING_TO_CLICK_TERMINALS:
            raise ValueError(
                "nothing_to_click must be one of %s, got %r"
                % (sorted(_NOTHING_TO_CLICK_TERMINALS), nothing_to_click))
        return nothing_to_click
    if not budget_left:
        return TERMINAL_BUDGET
    return TERMINAL_LOOP

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
               max_steps: int = 0,
               outcome_milestone: Mapping[str, Any] | None = None,
               confirmation_rung: str = "",
               confirmation_detail: str = "") -> dict[str, Any]:
    """One recorded journey.

    ``completed`` is DERIVED from the terminal reason, never passed in — the whole
    point is that a truncated walk cannot be reported as a covered journey.

    ``journey_completed`` (A4.3 / T-AC-06) is a STRICTLY STRONGER claim and is
    derived from something else entirely: the linked outcome milestone's own
    ``verified`` flag, which is itself a function of the observed transition.
    The two must not be conflated —

        completed          the funnel was WALKED to its natural end. True at a
                           submit boundary the crawl never crossed.
        journey_completed  the irreversible boundary was CROSSED under an
                           explicit approval and the far side was verified to be
                           a genuine confirmation. This is what "we complete real
                           customer journeys" means, and nothing else is.

    No counter participates in either. ``forms_submitted`` counts attempts and
    is incremented whatever the outcome, so nine submits that all errored score
    exactly as nine completed applications — which is why it is not consulted
    here and why a test pins that it cannot be.
    """
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
        #: A4.3 — the journey's outcome milestone, or ``None``. Its presence
        #: means an approved irreversible boundary was crossed on this journey;
        #: its ``verified`` flag means the far side was a genuine confirmation.
        "outcome_milestone": (dict(outcome_milestone)
                              if isinstance(outcome_milestone, Mapping) else None),
        #: THE PRODUCT CLAIM, and the only field that may carry it. Derived from
        #: the milestone's own observation — never from a terminal, never from a
        #: counter, and never settable by a caller.
        "journey_completed": bool(
            isinstance(outcome_milestone, Mapping)
            and outcome_milestone.get("verified")),
        #: M1.4 — WHAT the application said, and on which rung it said it, when
        #: this journey ended at a recognized confirmation. Both empty on every
        #: other terminal (and therefore on every flow recorded before this
        #: milestone), so a reader can never mistake "we inferred completion"
        #: for "the app declared it" — the evidence travels with the claim.
        **({"confirmation_rung": str(confirmation_rung)[:40],
            "confirmation_detail": str(confirmation_detail)[:300]}
           if term == TERMINAL_CONFIRMATION and confirmation_rung else {}),
    }


def journeys_completed(flows: Sequence[Mapping[str, Any]]) -> int:
    """How many journeys were completed END TO END — the product claim.

    Deliberately a function over the FLOWS rather than a counter maintained
    during the crawl. A counter can be incremented from anywhere and has been:
    ``forms_submitted`` rises on every attempt, error or not. This can only be
    computed from journeys that carry a verified outcome milestone, so there is
    no line of code anywhere that can raise it without an observation behind it.
    """
    return sum(1 for f in (flows or ()) if isinstance(f, Mapping)
               and f.get("journey_completed"))


def summarize(flows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The coverage claim, stated so it cannot be read as more than it is.

    ``branch_coverage`` is reported as ``False`` explicitly rather than omitted: a
    reader must not be able to infer that walking every flow once means every
    business path was covered. One path per funnel is one path, and the decision
    points that fork the funnel have not been explored."""
    total = len(flows)
    done = [f for f in flows if f.get("completed")]
    journeys = [f for f in flows if f.get("journey_completed")]
    crossed = [f for f in flows if isinstance(f.get("outcome_milestone"), Mapping)]
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
    # T-SI-06 — see the reported keys below for why one integer was not enough.
    def _steps(f: Mapping[str, Any]) -> int:
        return int(f.get("step_count") or 0)

    deepest_steps = max((_steps(f) for f in flows), default=0)
    deepest_proven = max((_steps(f) for f in done), default=0)
    # The deepest flow, tie-broken toward a COMPLETED one: if a six-step walk
    # finished and another six-step walk was cut off, the depth six IS proven,
    # and reporting it as capped would understate what the crawl established.
    deepest = max(flows, key=lambda f: (_steps(f), bool(f.get("completed"))),
                  default=None) if flows else None
    deepest_terminal = str((deepest or {}).get("terminal") or "")
    deepest_capped = bool(deepest is not None and not deepest.get("completed"))
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
        # ── A4.3 / T-AC-06: THE THREE NUMBERS, AND WHY THERE ARE THREE ───────
        # `flows_completed` says the funnel was walked to an end — which
        # includes stopping politely in front of a submit button.
        # `boundaries_crossed` says an approved irreversible click actually
        # fired. `journeys_completed` says the far side was OBSERVED to be a
        # confirmation. Collapsing any two of these is how "we complete customer
        # journeys" gets claimed on the strength of a crawl that never crossed
        # anything, or on one that crossed and then errored.
        "boundaries_crossed": len(crossed),
        "journeys_completed": len(journeys),
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
        # ── T-SI-06: DEPTH THAT MEANS SOMETHING ──────────────────────────────
        #
        # ``deepest_flow_steps`` answers "how many steps did the deepest walk
        # take", and on its own that number cannot be read. Six steps because
        # the application has six, and six steps because the walk was cut off at
        # six, are the same integer and opposite facts — the first is full
        # coverage of a short funnel, the second is unknown coverage of a funnel
        # of unknown length. A golden gate asserting ``deepest_flow >= 5`` passes
        # identically on both, which is how a traversal cap was mistaken for an
        # application's shape.
        #
        # These four report the depth the crawl actually PROVED, whether the
        # deepest walk was capped, and by what — so a shallow application and a
        # truncated traversal can never be read as the same result again.
        "deepest_flow_steps": deepest_steps,
        #: The deepest journey walked to a genuine end (submit boundary or
        #: nothing left to advance). THIS is the application's proven depth: no
        #: budget was involved, so the funnel really was this long.
        "deepest_flow_proven_steps": deepest_proven,
        #: True when the deepest journey stopped because it ran out of budget
        #: rather than out of funnel. The application is at least this deep and
        #: possibly deeper; ``deepest_flow_steps`` is a floor, not a measurement.
        "deepest_flow_capped": deepest_capped,
        #: The terminal of the deepest journey, so a reader never has to guess
        #: which of the two cases above produced the number.
        "deepest_flow_terminal": deepest_terminal,
        # THE HONESTY FLAG. One path per funnel was walked. Which option was taken
        # at each decision point was decided once, and the alternatives — a
        # different premium, a different eligibility outcome — were never visited.
        "branch_coverage": False,
        "branch_coverage_note": (
            "One path per journey. At each decision point a single option was taken, "
            "so business paths behind the other options were not visited."
        ),
    }

"""Typed, durable crawl diagnosis (Phase 0 — legible failure).

A crawl can end in many honest states — refused, failed on login, completed but
blocked on seed fields, completed but yielding zero cases, or genuinely done. Those
signals are scattered across ``qe_explorations.status`` / ``.error`` and the ``stats``
JSONB (``coverage.fields_needing_seed``, ``generate.generated`` / ``.no_cases_reason``,
``visits``). This module folds the signals that are ACTUALLY PRESENT on a row into ONE
typed outcome — ``{code, severity, title, human, remediation, fields, evidence}`` — so
the portal can always tell a client WHAT happened and WHAT to do next, and never show a
blank Test Studio with no stated reason (the showstopper this phase closes).

Doctrine (never green-wash): the classifier only ever labels evidence that is genuinely
present on the row. Anything it cannot recognise becomes ``UNCLASSIFIED`` carrying the
raw error verbatim — it never invents a friendlier reason. Login-failure is matched only
on conservative, explicit tokens so an unrelated failure is never mislabelled a login
problem; ambiguous cases fall through to ``FAILED`` / ``UNCLASSIFIED`` with the raw text.

It is a PURE function (no DB, no clock, no I/O), so it is exhaustively unit-testable and
deterministic — the same row snapshot always yields the same diagnosis.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .assist_classifier import fill_headline, summarize_fill

# ── Diagnosis codes — stable strings the portal keys its UI on ────────────────
CODE_COMPLETED_OK = "COMPLETED_OK"
CODE_SEEDS_NEEDED = "SEEDS_NEEDED"
CODE_NO_CASES = "NO_CASES"
CODE_EMPTY_SUBSTRATE = "EMPTY_SUBSTRATE"
CODE_LOGIN_FAILED = "LOGIN_FAILED"
CODE_STALLED = "STALLED"
CODE_REFUSED = "REFUSED"
CODE_FAILED = "FAILED"
CODE_RUNNING = "RUNNING"
CODE_QUEUED = "QUEUED"
CODE_NONE = "NONE"
CODE_UNCLASSIFIED = "UNCLASSIFIED"
#: One or more journeys ended ``oracle_unavailable`` — the PLATFORM's advance
#: service could not be reached mid-walk. Never the app's fault; the honest
#: remedy is a re-crawl once the service is healthy.
CODE_ADVANCE_ORACLE_UNAVAILABLE = "ADVANCE_ORACLE_UNAVAILABLE"

# ── R2 named-stop codes (derived from R0/R1 evidence) ───────────────────────
#: Controls whose intent was never met after the full R1 interaction ladder —
#: the crawl tried every available mechanic and the control still refused to
#: commit the intended value.  Names the blocked controls so the operator
#: knows WHAT to investigate, not just that something failed.
CODE_INTERACTION_BLOCKED = "INTERACTION_BLOCKED"
#: Journeys that could not advance past a step where required fields went
#: unanswered — the form's own validation prevented the Continue/Next from
#: working.  Distinct from SEEDS_NEEDED (which is about the RESIDUE ask):
#: this names the journeys the validation blocked.
CODE_WALK_BLOCKED_VALIDATION = "WALK_BLOCKED_VALIDATION"
#: Enumerable decision points (radio groups, selects) were discovered but
#: had no available value — the business-path forks behind each option were
#: never explored.  More specific than SEEDS_NEEDED: it names the forks and
#: their options, not just the field names.
CODE_DECISION_UNRESOLVED = "DECISION_UNRESOLVED"

#: Terminal codes that mean "the client should look" (a problem or an action).
TERMINAL_ATTENTION_CODES = frozenset({
    CODE_SEEDS_NEEDED, CODE_NO_CASES, CODE_EMPTY_SUBSTRATE, CODE_LOGIN_FAILED,
    CODE_STALLED, CODE_REFUSED, CODE_FAILED, CODE_UNCLASSIFIED,
    CODE_ADVANCE_ORACLE_UNAVAILABLE,
    CODE_INTERACTION_BLOCKED, CODE_WALK_BLOCKED_VALIDATION,
    CODE_DECISION_UNRESOLVED,
})

# Severity the UI can style: ok (green), info (in-progress), action (a human must
# do something), warn (a problem with no single obvious next action).
SEV_OK = "ok"
SEV_INFO = "info"
SEV_ACTION = "action"
SEV_WARN = "warn"

# Non-terminal statuses (a crawl still in flight). 'queued'/'claimed' are the
# Phase-2 durable-queue states; they are surfaced honestly as "waiting", never
# as a failure. 'stalled' is a first-class terminal-for-UI state written by the
# reaper / read-time stall valve.
_ACTIVE = frozenset({"pending", "writing", "running", "dispatched"})
_QUEUED = frozenset({"queued", "claimed"})

# Conservative login-wall tokens — matched ONLY in the durable error / stop-reason
# text. Kept explicit so an unrelated failure is never confidently mislabelled a
# login problem; anything not matched falls through to FAILED with the raw reason.
_LOGIN_TOKENS = (
    "login failed", "authentication failed", "auth failed", "invalid credential",
    "invalid credentials", "invalid login", "bad credential", "unauthorized",
    "sign in failed", "sign-in failed", "could not authenticate",
    "authentication required", "login_wall", "auth_wall", "http 401", " 401 ",
)

# The crawler's own terminal stop-reason for an unverified sign-in. A login-blocked
# crawl reports status=COMPLETED (it wrote an honest 1-page substrate) with this
# stop_reason — so the login wall must be detected here too, or it masquerades as a
# confusingly empty "nothing captured" / "needs seeds" result.
_LOGIN_STOP_REASONS = frozenset({"auth_failed"})


def _stats_of(stats: Any) -> Mapping:
    return stats if isinstance(stats, Mapping) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build(
    code: str, severity: str, title: str, human: str,
    remediation: str = "", fields: list | None = None,
    evidence: Mapping | None = None,
) -> dict:
    """A JSON-serialisable diagnosis record (safe to embed in an API response)."""
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "human": human,
        "remediation": remediation,
        "fields": [str(f) for f in (fields or [])],
        "evidence": dict(evidence or {}),
    }


def diagnose(*, status: str | None, error: str | None = "", stats: Any = None) -> dict:
    """Classify a crawl's terminal (or in-flight) state into one typed diagnosis.

    Reads ONLY the fields a real ``qe_explorations`` row carries: the (already
    stall-valve-adjusted) ``status``, the honest ``error`` string, and the ``stats``
    JSONB (``coverage.fields_needing_seed``, ``generate.generated`` /
    ``.no_cases_reason``, ``visits``, ``stop_reason``). Pure and deterministic.
    """
    st = (status or "unknown").strip().lower()
    s = _stats_of(stats)
    err = (error or "").strip()

    cov = s.get("coverage") if isinstance(s.get("coverage"), Mapping) else {}
    gen = s.get("generate") if isinstance(s.get("generate"), Mapping) else {}
    seed_fields = [str(f).strip() for f in (cov.get("fields_needing_seed") or []) if str(f).strip()]
    _generated = gen.get("generated")
    generated_n = _int(_generated, 0)
    no_cases_reason = str(gen.get("no_cases_reason") or "").strip()
    visits = _int(s.get("visits"), 0)
    # error + stop_reason together form the durable failure text (the callback folds
    # stop_reason into error for the no-manifest path; stats may also carry it).
    reason_text = (err + " " + str(s.get("stop_reason") or "")).strip().lower()

    # ── In-flight / not-yet-run (never a failure) ─────────────────────────────
    if st in _QUEUED:
        return _build(CODE_QUEUED, SEV_INFO, "Queued",
                      "This crawl is waiting for a free worker.",
                      "It will start automatically — no action needed.")
    if st in _ACTIVE:
        return _build(CODE_RUNNING, SEV_INFO, "Crawling…",
                      "The crawl is in progress.",
                      "No action needed — results appear when it finishes.")
    if st in ("none", "unknown") and not err and not s:
        return _build(CODE_NONE, SEV_INFO, "No crawl yet",
                      "This app has not been crawled.",
                      "Start a crawl to discover its flows.")

    # ── Refusal — first-class, carries the reason verbatim ────────────────────
    if st == "refused":
        return _build(CODE_REFUSED, SEV_WARN, "Crawl refused",
                      err or "The crawl was refused — no honest substrate could be written.",
                      "Review the reason, adjust the app configuration, and re-crawl.",
                      evidence={"error": err})

    # ── Stalled — a crashed worker / lost callback (reaper or read-time valve) ─
    if st == "stalled" or reason_text.startswith("stalled:"):
        return _build(CODE_STALLED, SEV_WARN, "Crawl stalled",
                      "The crawl stopped responding — a crashed worker or a lost signal.",
                      "Re-run the crawl.",
                      evidence={"error": err})

    # ── Login wall — a scripted sign-in could not complete ────────────────────
    # Fires on a FAILED crawl OR a COMPLETED one whose stop_reason is auth_failed
    # (the crawl reached the login page, filled it, but the app didn't advance — it
    # never got past login to the app's flows). Checked BEFORE the completed-grading
    # below so it never masquerades as "nothing captured" / "needs seeds", which is
    # exactly the confusing dead-end this avoids. A crawl that still produced cases is
    # left to grade as productive (an auth wall deeper in, not a hard block).
    stop_reason = str(s.get("stop_reason") or "").strip().lower()
    # The crawler's own auth_failed stop-reason is the RELIABLE signal (set only when a
    # login was actually attempted and didn't verify). The generic HTTP tokens (401 /
    # "unauthorized") can come from a stray sub-resource on a LOGIN-LESS app, so only trust
    # them on an outright FAILED crawl — never to relabel a COMPLETED login-less crawl.
    login_wall = stop_reason in _LOGIN_STOP_REASONS or (
        st == "failed" and any(t in reason_text for t in _LOGIN_TOKENS)
    )
    if login_wall and generated_n <= 0:
        return _build(CODE_LOGIN_FAILED, SEV_ACTION, "Login blocked",
                      "The crawl reached the app's login page but couldn't sign in, so it "
                      "couldn't get past it to test the app's flows.",
                      "Update the app's login credentials (or provide a signed-in session), "
                      "then re-crawl.",
                      evidence={"error": err, "stop_reason": stop_reason})

    # ── Failed — a generic failure (login walls handled above) ────────────────
    if st == "failed":
        return _build(CODE_FAILED, SEV_WARN, "Crawl failed",
                      err or "The crawl failed before producing a result.",
                      "Review the reason and re-crawl.",
                      evidence={"error": err})

    # ── Completed — grade it honestly (order = most-actionable first) ─────────
    if st == "completed":
        if visits <= 0:
            return _build(CODE_EMPTY_SUBSTRATE, SEV_WARN, "Nothing was captured",
                          "The crawl completed but captured no pages.",
                          "Check the URL is reachable and public, then re-crawl.",
                          evidence={"visits": 0})
        # Journeys that stopped because OUR advance service was unreachable are
        # a PLATFORM failure, stated before any green: an E2E crawl whose
        # journeys silently did not finish is exactly the green-wash this
        # product exists to prevent. Never the app's fault.
        flow_summary = (cov.get("flow_summary")
                        if isinstance(cov.get("flow_summary"), Mapping) else {})
        trunc_reasons = (flow_summary.get("truncation_reasons")
                         if isinstance(flow_summary.get("truncation_reasons"), Mapping)
                         else {})
        oracle_unavail_n = _int(trunc_reasons.get("oracle_unavailable"), 0)
        if oracle_unavail_n > 0:
            return _build(
                CODE_ADVANCE_ORACLE_UNAVAILABLE, SEV_ACTION,
                "Journeys not proven — platform service was unavailable",
                f"{oracle_unavail_n} journey walk"
                f"{'s' if oracle_unavail_n != 1 else ''} stopped because the "
                "platform's advance-decision service could not be reached "
                "mid-crawl. This is a platform-side failure — not a problem "
                "with your application — and those journeys are reported as "
                "NOT proven complete rather than green.",
                "Re-crawl once the platform advance service is healthy; the "
                "journeys will be walked to their real end.",
                evidence={"oracle_unavailable_journeys": oracle_unavail_n,
                          "visits": visits, "generated": generated_n})
        # ── R2 named stops — derived from R0/R1 evidence, non-productive only ──
        # These fire BEFORE COMPLETED_OK so a crawl blocked by interaction or
        # validation tells the operator WHY it produced nothing, not just that it
        # produced nothing.  A productive crawl (generated > 0) is always OK —
        # blocked controls deeper in the funnel are noted in evidence, not an alarm.
        intent_unmet_n = _int(flow_summary.get("intent_unmet"), 0)
        if intent_unmet_n > 0 and generated_n <= 0:
            fl_raw = (cov.get("field_ledger")
                      if isinstance(cov.get("field_ledger"), list) else [])
            blocked = [str(e.get("name") or "")[:120] for e in fl_raw
                       if isinstance(e, Mapping)
                       and str(e.get("provenance") or "") == "intent_unmet"]
            blocked_names = ", ".join(blocked[:8]) or "unnamed controls"
            return _build(
                CODE_INTERACTION_BLOCKED, SEV_ACTION,
                "Controls could not be operated",
                f"{intent_unmet_n} control"
                f"{'s' if intent_unmet_n != 1 else ''} "
                f"did not accept "
                f"{'their' if intent_unmet_n != 1 else 'its'} "
                f"intended value despite trying all available mechanics: "
                f"{blocked_names}.",
                "These controls may use custom widgets the crawler's "
                "interaction ladder does not yet handle. Check the control "
                "types and provide guidance if needed, then re-crawl.",
                evidence={"intent_unmet": intent_unmet_n,
                          "blocked_controls": blocked[:8],
                          "visits": visits, "generated": generated_n})

        flows_raw = (cov.get("flows")
                     if isinstance(cov.get("flows"), list) else [])
        validation_blocked = [
            f for f in flows_raw
            if isinstance(f, Mapping) and not f.get("completed")
            and _int(f.get("fields_unanswered"), 0) > 0
        ]
        if validation_blocked and generated_n <= 0:
            n_blocked = len(validation_blocked)
            total_unanswered = sum(
                _int(f.get("fields_unanswered"), 0)
                for f in validation_blocked)
            return _build(
                CODE_WALK_BLOCKED_VALIDATION, SEV_ACTION,
                "Journeys blocked by form validation",
                f"{n_blocked} journey"
                f"{'s' if n_blocked != 1 else ''} "
                f"could not advance past a step where "
                f"{total_unanswered} required field"
                f"{'s' if total_unanswered != 1 else ''} "
                f"went unanswered — the form's validation prevented progress.",
                "Provide values for the fields the crawl could not fill "
                "(listed in the seed residue), then re-crawl to walk the "
                "full funnels.",
                fields=seed_fields,
                evidence={"validation_blocked_flows": n_blocked,
                          "total_unanswered": total_unanswered,
                          "visits": visits, "generated": generated_n})

        # WHAT THE AGENT ANSWERED, and why anything is left. Computed once and
        # reused by the messages below, so a crawl never reports what it NEEDS
        # without first reporting what it DID.
        fill = summarize_fill(cov)
        assist_names = [a["name"] for a in fill["needs_assistance"] if a.get("name")]

        if generated_n > 0:
            # A productive crawl reads as OK. It leads with the fields the agent
            # populated itself, because that is the larger and more
            # decision-relevant number — the old copy led with the ask, which read
            # as though the crawl had achieved nothing.
            #
            # Nothing here asks for a re-crawl: the pages are already catalogued,
            # so a value supplied now is applied to what the crawl already knows.
            # A DATA gap must never force a DISCOVERY restart.
            # Prefer the classified list; fall back to the crawler's own residue
            # when no field ledger reached us (an older manifest, or a crawl that
            # recorded seeds without per-field rows). Losing the hint entirely
            # would be a regression — a value the operator could supply today
            # would simply stop being mentioned.
            remedy_names = assist_names or seed_fields
            remedy = ""
            if remedy_names:
                remedy = (f"Supply {', '.join(remedy_names[:12])} to reach the flows "
                          f"behind them — the pages are already catalogued.")
            return _build(CODE_COMPLETED_OK, SEV_OK, "Ready",
                          f"The crawl completed and generated {generated_n} test case"
                          f"{'s' if generated_n != 1 else ''}. "
                          + fill_headline(fill),
                          remedy,
                          fields=assist_names or seed_fields,
                          evidence={"visits": visits, "generated": generated_n,
                                    "auto_filled": fill["auto_filled"],
                                    "fill_provenance": fill["provenance"],
                                    "needs_assistance": fill["needs_assistance"],
                                    "agent_gaps": fill["agent_gaps"]})

        # R2: DECISION_UNRESOLVED — enumerable decision forks that had no
        # available value.  More specific than SEEDS_NEEDED because it names the
        # business-path options the crawl could not explore.
        fl_raw = (cov.get("field_ledger")
                  if isinstance(cov.get("field_ledger"), list) else [])
        unresolved = [
            e for e in fl_raw
            if isinstance(e, Mapping)
            and str(e.get("provenance") or "") == "needs_input"
            and isinstance(e.get("options"), list) and len(e.get("options", []))
        ]
        if unresolved:
            dp_names = [str(e.get("name") or "")[:120] for e in unresolved[:8]]
            dp_display = ", ".join(dp_names) or "unnamed decision points"
            n_dp = len(unresolved)
            return _build(
                CODE_DECISION_UNRESOLVED, SEV_ACTION,
                "Decision points need answers",
                f"{n_dp} decision point"
                f"{'s' if n_dp != 1 else ''} "
                f"{'were' if n_dp != 1 else 'was'} discovered but "
                f"no value was available: {dp_display}. "
                f"The business paths behind each option were not explored.",
                f"Provide values for: {dp_display}. Then re-crawl — each "
                f"option opens a different business path the crawler will walk.",
                fields=[str(e.get("name") or "") for e in unresolved[:12]],
                evidence={"unresolved_decisions": n_dp,
                          "decision_names": dp_names,
                          "visits": visits, "generated": generated_n})

        if seed_fields:
            # Say what was populated BEFORE what is missing, name only what a
            # person genuinely has to supply, and never demand a re-crawl — a data
            # gap must not force a discovery restart of pages already catalogued.
            names = ", ".join(assist_names[:12]) or ", ".join(seed_fields[:12])
            return _build(CODE_SEEDS_NEEDED, SEV_ACTION, "A few values needed",
                          fill_headline(fill)
                          + f" The flows behind {names} were not reached.",
                          f"Supply {names} — the pages are already catalogued, so the "
                          f"values are applied to what the crawl already found.",
                          fields=assist_names or seed_fields,
                          evidence={"visits": visits,
                                    "auto_filled": fill["auto_filled"],
                                    "fill_provenance": fill["provenance"],
                                    "needs_assistance": fill["needs_assistance"],
                                    "agent_gaps": fill["agent_gaps"]})
        return _build(CODE_NO_CASES, SEV_WARN, "No test cases yet",
                      no_cases_reason or "The crawl completed but did not produce runnable test cases.",
                      "Review the discovered flows; a seeded or deeper crawl may be needed.",
                      evidence={"visits": visits, "generated": 0, "no_cases_reason": no_cases_reason})

    # ── Anything else — honest UNCLASSIFIED, raw error verbatim (never invented) ─
    return _build(CODE_UNCLASSIFIED, SEV_WARN, "Unrecognised state",
                  err or f"The crawl ended in an unrecognised state ({st}).",
                  "Review the raw reason; re-crawl if needed.",
                  evidence={"status": st, "error": err})

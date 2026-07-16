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

#: Terminal codes that mean "the client should look" (a problem or an action).
TERMINAL_ATTENTION_CODES = frozenset({
    CODE_SEEDS_NEEDED, CODE_NO_CASES, CODE_EMPTY_SUBSTRATE, CODE_LOGIN_FAILED,
    CODE_STALLED, CODE_REFUSED, CODE_FAILED, CODE_UNCLASSIFIED,
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

    # ── Failed — distinguish a login wall from a generic failure ──────────────
    if st == "failed":
        if any(tok in reason_text for tok in _LOGIN_TOKENS):
            return _build(CODE_LOGIN_FAILED, SEV_ACTION, "Login failed",
                          "The crawl could not sign in to the app.",
                          "Update the app's login credentials, then re-crawl.",
                          evidence={"error": err})
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
        if generated_n > 0:
            # A productive crawl reads as OK; any remaining seed fields are surfaced
            # only as an optional "go deeper" hint, never as an alarm.
            return _build(CODE_COMPLETED_OK, SEV_OK, "Ready",
                          f"The crawl completed and generated {generated_n} test case"
                          f"{'s' if generated_n != 1 else ''}.",
                          (f"Provide values for {', '.join(seed_fields[:12])} to reach deeper flows."
                           if seed_fields else ""),
                          fields=seed_fields,
                          evidence={"visits": visits, "generated": generated_n})
        if seed_fields:
            names = ", ".join(seed_fields[:12])
            return _build(CODE_SEEDS_NEEDED, SEV_ACTION, "A few values needed",
                          f"The crawl explored the app but needs real values to go deeper: {names}.",
                          f"Provide values for: {names}. Then re-crawl to reach the flows behind them.",
                          fields=seed_fields,
                          evidence={"visits": visits})
        return _build(CODE_NO_CASES, SEV_WARN, "No test cases yet",
                      no_cases_reason or "The crawl completed but did not produce runnable test cases.",
                      "Review the discovered flows; a seeded or deeper crawl may be needed.",
                      evidence={"visits": visits, "generated": 0, "no_cases_reason": no_cases_reason})

    # ── Anything else — honest UNCLASSIFIED, raw error verbatim (never invented) ─
    return _build(CODE_UNCLASSIFIED, SEV_WARN, "Unrecognised state",
                  err or f"The crawl ended in an unrecognised state ({st}).",
                  "Review the raw reason; re-crawl if needed.",
                  evidence={"status": st, "error": err})

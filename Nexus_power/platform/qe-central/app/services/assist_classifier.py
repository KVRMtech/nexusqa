"""WHY a field needs a human — so asking becomes the exception, not the default.

The old shape was: any field the crawl could not fill became "provide a value and
re-crawl". Every gap looked identical, so a genuine limit ("this needs a real
policy number that exists in your system") was presented with exactly the same
weight as a field the agent simply failed to derive — and the client was asked to
do the platform's work in both cases.

There are only a few reasons a person must be involved. This module names them:

  * ``enterprise_account`` — an identifier that must EXIST in the client's system
    (policy / member / claim / account number). No generator can invent one that
    resolves; inventing it produces a test that passes against nothing.
  * ``second_factor``      — a one-time code. It is one-time BY DESIGN.
  * ``captcha``            — a challenge whose entire purpose is to refuse a bot.
  * ``hardware_token``     — approval on a device the platform cannot hold.
  * ``legal_approval``     — a signature/consent that carries legal weight.

Anything else is ``agent_gap``: a field the agent SHOULD have been able to answer
and did not. That is a defect on our side, and it is reported as one rather than
quietly billed to the client as a chore. Counting those honestly is what makes
the residue list shrink over time instead of becoming furniture.

Pure functions over the crawl's own field ledger — no LLM, no network, no DB.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# ── the five reasons a human is genuinely required ──────────────────────────
ASSIST_ENTERPRISE_ACCOUNT = "enterprise_account"
ASSIST_SECOND_FACTOR = "second_factor"
ASSIST_CAPTCHA = "captcha"
ASSIST_HARDWARE_TOKEN = "hardware_token"
ASSIST_LEGAL_APPROVAL = "legal_approval"
#: NOT a reason to ask — a gap in the agent, recorded as such.
ASSIST_AGENT_GAP = "agent_gap"

HUMAN_REQUIRED = frozenset({
    ASSIST_ENTERPRISE_ACCOUNT, ASSIST_SECOND_FACTOR, ASSIST_CAPTCHA,
    ASSIST_HARDWARE_TOKEN, ASSIST_LEGAL_APPROVAL,
})

#: One line per reason, addressed to the person reading the app — it says why the
#: agent stopped, never what the agent failed at.
ASSIST_EXPLANATION = {
    ASSIST_ENTERPRISE_ACCOUNT:
        "needs an identifier that already exists in your system — a generated one "
        "would not resolve",
    ASSIST_SECOND_FACTOR:
        "a one-time code, which is single-use by design",
    ASSIST_CAPTCHA:
        "a challenge designed to refuse automation",
    ASSIST_HARDWARE_TOKEN:
        "approval on a device the platform cannot hold",
    ASSIST_LEGAL_APPROVAL:
        "a signature or consent that carries legal weight",
    ASSIST_AGENT_GAP:
        "the agent could not derive a value for this yet",
}

#: Semantic types the explorer already classifies, mapped straight through. These
#: are the strongest signal available and are checked before any label matching.
_SEMANTIC_ASSIST = {
    "one_time_code": ASSIST_SECOND_FACTOR,
}

# Label patterns. Deliberately narrow: a false ENTERPRISE_ACCOUNT reads as "we
# cannot do this for you" about a field we could in fact have filled, so anything
# ambiguous falls through to agent_gap where it stays visible as our problem.
# Word-boundary anchored so "account type" or "policy term" never match.
#
# The suffix is matched with (?!\w) rather than a trailing \b: "#" is not a word
# character, so "\b#\b" never matches "Member #" — the exact label a member
# portal uses. (?!\w) also keeps "Certificate No" matching while "Certificate
# Node" does not.
_ACCOUNT_RE = re.compile(
    r"\b(?:policy|member|membership|claim|contract|certificate|subscriber|"
    r"account|customer|reference|case|group)\s*"
    r"(?:#|(?:number|num|no|id)(?!\w))",
    re.IGNORECASE)
_CAPTCHA_RE = re.compile(r"\bcaptcha\b|\brecaptcha\b|\bhcaptcha\b|\bi'?m not a robot\b",
                         re.IGNORECASE)
_TOKEN_RE = re.compile(
    r"\b(hardware|security)\s+(token|key)\b|\byubi\s*key\b|\bsmart\s*card\b"
    r"|\bapprove\s+(?:on|in)\s+(?:your\s+|the\s+)?(?:device|app|phone)\b"
    r"|\bpush\s+notification\b",
    re.IGNORECASE)
_OTP_RE = re.compile(
    r"\bone[- ]?time\b|\bverification\s+code\b|\bsecurity\s+code\b|\bauth(entication)?\s+code\b"
    r"|\bsms\s+code\b|\bmfa\b|\b2fa\b|\botp\b|\bpasscode\s+sent\b",
    re.IGNORECASE)
_LEGAL_RE = re.compile(
    r"\be[- ]?sign\b|\belectronic\s+signature\b|\bsignature\b|\bsign\s+here\b"
    r"|\battest\b|\bcertify\b|\bunder\s+penalty\b|\blegally\s+bind",
    re.IGNORECASE)


def classify_assist_reason(entry: Mapping[str, Any]) -> str:
    """Why this unfilled field needs a person — one of the reasons above.

    ``entry`` is a crawl field-ledger row (``{name, semantic_type, ...}``).
    Defaults to :data:`ASSIST_AGENT_GAP`, because assuming a human is required is
    exactly how asking became the default in the first place.
    """
    semantic = str(entry.get("semantic_type") or "").strip().lower()
    mapped = _SEMANTIC_ASSIST.get(semantic)
    if mapped:
        return mapped

    name = str(entry.get("name") or "")
    if not name.strip():
        return ASSIST_AGENT_GAP
    # Order matters where vocabularies overlap: a "security code" is a second
    # factor, while a "security token" is hardware — the more specific device
    # sense is tested first.
    if _CAPTCHA_RE.search(name):
        return ASSIST_CAPTCHA
    if _TOKEN_RE.search(name):
        return ASSIST_HARDWARE_TOKEN
    if _OTP_RE.search(name):
        return ASSIST_SECOND_FACTOR
    if _LEGAL_RE.search(name):
        return ASSIST_LEGAL_APPROVAL
    if _ACCOUNT_RE.search(name):
        return ASSIST_ENTERPRISE_ACCOUNT
    return ASSIST_AGENT_GAP


def summarize_fill(coverage: Mapping[str, Any] | None) -> dict[str, Any]:
    """What the agent answered, what it could not, and why — from one crawl.

    Returns::

        {"discovered": int,      # fillable fields the crawl met
         "auto_filled": int,     # answered without asking anyone
         "needs_assistance": [{"name", "reason", "explanation"}],
         "agent_gaps": [...],    # our problem, not the client's
         "provenance": {...}}    # auto_filled broken down by where each came from

    ``needs_assistance`` holds ONLY the genuinely-human reasons. A field the agent
    merely failed to derive is separated into ``agent_gaps`` so it is never billed
    to the client as a chore, and so the two can be tracked apart over time.
    """
    cov = coverage if isinstance(coverage, Mapping) else {}
    ledger = cov.get("field_ledger")
    if not isinstance(ledger, Sequence) or isinstance(ledger, (str, bytes)):
        ledger = []

    auto = 0
    provenance: dict[str, int] = {}
    assist: list[dict[str, str]] = []
    gaps: list[dict[str, str]] = []
    seen: set[str] = set()
    discovered = 0

    for entry in ledger:
        if not isinstance(entry, Mapping):
            continue
        discovered += 1
        if entry.get("filled"):
            auto += 1
            prov = str(entry.get("provenance") or "unknown")
            provenance[prov] = provenance.get(prov, 0) + 1
            continue
        name = str(entry.get("name") or "")[:200]
        # The same question met on several pages is ONE thing to ask about.
        key = name.strip().lower() or str(entry.get("signature") or "")
        if key in seen:
            continue
        seen.add(key)
        reason = classify_assist_reason(entry)
        row = {"name": name, "reason": reason,
               "explanation": ASSIST_EXPLANATION.get(reason, "")}
        (assist if reason in HUMAN_REQUIRED else gaps).append(row)

    return {
        "discovered": discovered,
        "auto_filled": auto,
        "needs_assistance": assist,
        "agent_gaps": gaps,
        "provenance": provenance,
    }


def fill_headline(summary: Mapping[str, Any]) -> str:
    """One sentence a person can act on, stating what was done before what is left.

    The old copy led with the ask ("provide 15 values"), which reads as though the
    crawl achieved nothing. Leading with the work already done is not spin — it is
    the larger and more decision-relevant number.
    """
    auto = int(summary.get("auto_filled") or 0)
    assist = list(summary.get("needs_assistance") or ())
    gaps = list(summary.get("agent_gaps") or ())
    total = auto + len(assist) + len(gaps)
    if not total:
        return "No form fields were met on this crawl."

    parts = [f"Automatically populated {auto} of {total} field"
             f"{'s' if total != 1 else ''}."]
    if assist:
        names = ", ".join(a["name"] for a in assist[:6] if a.get("name"))
        parts.append(f"{len(assist)} need"
                     f"{'s' if len(assist) == 1 else ''} your input"
                     + (f": {names}." if names else "."))
    if gaps:
        # Named honestly as OUR gap. It is still worth showing — supplying a value
        # unblocks the flow today — but it is never presented as a limit of the app.
        parts.append(f"{len(gaps)} the agent could not derive yet "
                     f"(a gap on our side, not yours).")
    if not assist and not gaps:
        parts.append("Nothing needs your input.")
    return " ".join(parts)


__all__ = [
    "ASSIST_ENTERPRISE_ACCOUNT", "ASSIST_SECOND_FACTOR", "ASSIST_CAPTCHA",
    "ASSIST_HARDWARE_TOKEN", "ASSIST_LEGAL_APPROVAL", "ASSIST_AGENT_GAP",
    "HUMAN_REQUIRED", "ASSIST_EXPLANATION",
    "classify_assist_reason", "summarize_fill", "fill_headline",
]

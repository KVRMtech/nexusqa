"""Network / API oracle (Auto-Heal V0 — the cheapest, most deterministic real-bug signal).

A test step can fail because the BACKEND failed, not because a selector drifted. A 4xx/5xx
in the failing step's window — or an explicit server/gateway/network error in the runner's
error text — is a near-dispositive REAL APPLICATION BUG signal: the action executed, the
server rejected it. Promoting this from mere evidence to an adjudication axis lets us file
an honest, credible defect ("POST /confirm -> 503") and REFUSE to heal, instead of churning
selector recipes over a real outage.

Pure + stdlib-only. CONSERVATIVE: server_error / network_error are strong real-bug signals;
client_error (4xx) is advisory only (a 4xx may be intended validation), so it is NOT auto-
classified as a bug. Returns None when there is no grounded network signal (byte-identical
to today). The structured path is ready for when the runner emits per-run network entries;
until then ``detect`` falls back to explicit server/gateway/network phrasing in the error —
deliberately matching server-error WORDS, never bare digits ("500ms timeout" must NOT fire).
"""
from __future__ import annotations

import re

# Explicit, unambiguous server/gateway failure phrasing (not bare digits).
_SERVER_ERR_RE = re.compile(
    r"internal server error|service unavailable|bad gateway|gateway time-?out|"
    r"status(?: code)?:?\s*5\d\d|http/\d(?:\.\d)?\s*5\d\d|response\s*(?:status)?:?\s*5\d\d|"
    r"\b5\d\d\s+(?:server error|service|internal)\b",
    re.I,
)
_NETWORK_ERR_RE = re.compile(
    r"net::ERR_[A-Z_]+|ERR_CONNECTION|ECONNREFUSED|ENOTFOUND|ETIMEDOUT|EAI_AGAIN|"
    r"failed to fetch|connection (?:refused|reset|closed)|socket hang up|dns",
    re.I,
)
_CLIENT_ERR_RE = re.compile(
    r"status(?: code)?:?\s*4\d\d|http/\d(?:\.\d)?\s*4\d\d|response\s*(?:status)?:?\s*4\d\d|"
    r"\b4\d\d\s+(?:client error|bad request|unauthorized|forbidden|not found)\b",
    re.I,
)


def _excerpt(s: str, n: int = 220) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def classify_network_signal(entries, *, step_start_ms=None, step_end_ms=None) -> dict | None:
    """Structured path: scan captured network entries for the WORST 4xx/5xx in the step window.

    ``entries`` = list of {url, method, status, start_ms?, failed?/error?}. When a step window
    is given, entries outside it are ignored. Severity order: 5xx > network-failure > 4xx.
    Returns the signal dict or None. This is the real adjudication path once the runner emits
    per-run network entries; it stays inert (None) until then."""
    worst = None
    for e in (entries or []):
        if not isinstance(e, dict):
            continue
        if step_start_ms is not None and step_end_ms is not None:
            t = e.get("start_ms")
            if t is not None and not (step_start_ms <= t <= step_end_ms):
                continue
        try:
            status = int(e.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        url = str(e.get("url") or "")
        method = str(e.get("method") or "").upper()
        if 500 <= status <= 599:
            return {"kind": "server_error", "status": status, "url": url, "method": method,
                    "detail": f"{method} {url} -> {status}".strip()}
        if status == 0 and (e.get("failed") or e.get("error")):
            worst = worst or {"kind": "network_error", "url": url, "method": method,
                              "detail": f"{method} {url} -> network failure ({e.get('error') or 'failed'})".strip()}
        elif 400 <= status <= 499 and worst is None:
            worst = {"kind": "client_error", "status": status, "url": url, "method": method,
                     "detail": f"{method} {url} -> {status}".strip()}
    return worst


def network_signal_from_error(error_message: str) -> dict | None:
    """Text fallback (until the runner emits structured entries): fire ONLY on explicit
    server/gateway/network error phrasing. Conservative — a bare '500ms timeout' must NOT
    match (we require server-error words, not bare digits)."""
    err = error_message or ""
    if _SERVER_ERR_RE.search(err):
        return {"kind": "server_error", "detail": _excerpt(err)}
    if _NETWORK_ERR_RE.search(err):
        return {"kind": "network_error", "detail": _excerpt(err)}
    if _CLIENT_ERR_RE.search(err):
        return {"kind": "client_error", "detail": _excerpt(err)}
    return None


def detect(failure_record: dict, observed: dict | None = None) -> dict | None:
    """Best available network signal for a failing step: structured entries if the runner
    provided them (``failure_record['network']``), else the conservative error-text fallback."""
    fr = failure_record or {}
    entries = fr.get("network") or fr.get("network_entries")
    sig = classify_network_signal(entries) if entries else None
    return sig or network_signal_from_error(fr.get("error_message", "") or "")


def is_real_bug_signal(sig: dict | None) -> bool:
    """server_error / network_error => strong REAL-bug signal. client_error => advisory only
    (4xx may be intended validation) — NOT auto-classified as a bug."""
    return bool(sig and sig.get("kind") in ("server_error", "network_error"))

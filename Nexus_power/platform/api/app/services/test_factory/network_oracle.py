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
from urllib.parse import urlparse

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


def _same_app_host(host: str, base_host: str) -> bool:
    """True when ``host`` belongs to the app under test (exact or sub/super
    domain of the base host). Conservative string containment on dot
    boundaries — no PSL dependency."""
    h, b = (host or "").lower(), (base_host or "").lower()
    if not h or not b:
        return True  # no base to compare against — keep legacy behaviour
    return h == b or h.endswith("." + b) or b.endswith("." + h)


def classify_network_signal(entries, *, step_start_ms=None, step_end_ms=None,
                            base_host: str = "") -> dict | None:
    """Structured path: scan captured network entries for the WORST 4xx/5xx in the step window.

    ``entries`` = list of {url, method, status, start_ms?, failed?/error?}. When a step window
    is given, entries outside it are ignored. Severity order: 5xx > network-failure > 4xx.

    ORIGIN GATE (R7 — External Dependency Failure): when ``base_host`` is given,
    a failing request to a FOREIGN origin (analytics, CDN, payment sandbox…)
    never classifies as the application's own server error — it returns an
    advisory ``external_dependency`` signal instead (``is_real_bug_signal`` is
    False for it), and a same-origin signal always wins over a foreign one.
    Without ``base_host`` behaviour is unchanged (legacy callers).
    Returns the signal dict or None."""
    worst = None
    foreign = None
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
        if base_host and not _same_app_host(_host_of(url), base_host):
            # Foreign origin: record the worst as an advisory external signal.
            if (500 <= status <= 599) or (status == 0 and (e.get("failed") or e.get("error"))):
                foreign = foreign or {
                    "kind": "external_dependency", "status": status or None,
                    "url": url, "method": method,
                    "detail": (f"{method} {url} -> "
                               f"{status if status else 'network failure'} "
                               f"(third-party origin, not {base_host})").strip()}
            continue
        if 500 <= status <= 599:
            return {"kind": "server_error", "status": status, "url": url, "method": method,
                    "detail": f"{method} {url} -> {status}".strip()}
        if status == 0 and (e.get("failed") or e.get("error")):
            worst = worst or {"kind": "network_error", "url": url, "method": method,
                              "detail": f"{method} {url} -> network failure ({e.get('error') or 'failed'})".strip()}
        elif 400 <= status <= 499 and worst is None:
            worst = {"kind": "client_error", "status": status, "url": url, "method": method,
                     "detail": f"{method} {url} -> {status}".strip()}
    return worst or foreign


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


def detect(failure_record: dict, observed: dict | None = None,
           base_host: str = "") -> dict | None:
    """Best available network signal for a failing step: structured entries if the runner
    provided them (``failure_record['network']``), else the conservative error-text fallback.
    ``base_host`` (the app under test) enables the origin gate — a third-party
    5xx surfaces as ``external_dependency``, never as the app's own defect."""
    fr = failure_record or {}
    if not base_host:
        base_host = _host_of(str((observed or {}).get("url")
                                 or (observed or {}).get("next_url") or ""))
    entries = fr.get("network") or fr.get("network_entries")
    sig = classify_network_signal(entries, base_host=base_host) if entries else None
    return sig or network_signal_from_error(fr.get("error_message", "") or "")


def is_real_bug_signal(sig: dict | None) -> bool:
    """server_error / network_error => strong REAL-bug signal. client_error => advisory only
    (4xx may be intended validation) — NOT auto-classified as a bug."""
    return bool(sig and sig.get("kind") in ("server_error", "network_error"))


# Connection / DNS-class phrasing that means the HOST itself was unreachable
# (the app never answered), as opposed to a mid-flow request failing. These map
# to an ENVIRONMENT OUTAGE / precondition, not an application defect.
_CONN_DNS_RE = re.compile(
    r"ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_REFUSED|ERR_CONNECTION_TIMED_OUT|"
    r"ERR_CONNECTION_RESET|ERR_CONNECTION_CLOSED|ERR_ADDRESS_UNREACHABLE|"
    r"ERR_INTERNET_DISCONNECTED|ENOTFOUND|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|"
    r"EHOSTUNREACH|ENETUNREACH|\bdns\b|getaddrinfo|name (?:or service )?not known",
    re.I,
)


def _host_of(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in (url or "") else f"http://{url}")
        return (parsed.netloc or "").split("@")[-1].split(":")[0].lower().lstrip(".")
    except Exception:
        return ""


def is_base_host_connection_failure(
    sig: dict | None,
    *,
    base_url: str = "",
    is_first_step: bool = False,
) -> bool:
    """Distinguish a *base-host* connection/DNS failure (the app is unreachable —
    an ENVIRONMENT OUTAGE / precondition) from a mid-flow request failure.

    The caller (the auto-heal loop) treats ``network_error`` as a REAL_REGRESSION
    'Product Bug' via :func:`is_real_bug_signal`. That is wrong when the very
    first navigation to ``base_url`` can't connect or resolve DNS — nothing about
    the *product* failed; the target host was down/unreachable. This helper lets
    the caller route that case to a PRECONDITION escalation instead of filing an
    app defect. It NEVER green-washes: it only reclassifies the FAILURE channel
    (env vs app); the step still fails and still escalates to a human.

    Fires only for ``network_error`` (never ``server_error`` — a 5xx is a real
    app bug even on the base host) and only when the evidence points at the base
    host: either the signal's own ``url`` resolves to the same host as
    ``base_url``, or — when no per-request URL is available (text-fallback path)
    — the failure is connection/DNS-class AND this is the first step (so the only
    thing attempted was the base navigation).
    """
    if not sig or sig.get("kind") != "network_error":
        return False

    sig_url = str(sig.get("url") or "")
    base_host = _host_of(base_url)

    if sig_url:
        # Structured path: we know exactly which request failed.
        sig_host = _host_of(sig_url)
        return bool(base_host) and sig_host == base_host

    # Text-fallback path: no per-request URL. Treat a connection/DNS-class
    # failure on the FIRST step (where the only navigation is to base_url) as a
    # base-host outage. Conservative: a mid-flow network_error (is_first_step
    # False) stays an app-origin signal for the caller to adjudicate.
    detail = str(sig.get("detail") or "")
    return bool(is_first_step and _CONN_DNS_RE.search(detail))

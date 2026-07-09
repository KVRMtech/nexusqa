"""Deterministic secret/PII scrubber for provenance quotes and logs.

The repo-intel engine reads client SOURCE CODE — the single most
sensitive class of data in the whole platform. Every verbatim ``quote``
that is about to be persisted on an :class:`~app.extract.registry.Atom`,
and every string that might be logged, is passed through
:func:`scrub` FIRST. A hardcoded credential that a developer left in the
source must never resurface in our database or telemetry.

Design constraints (mirrors the SDK's ``llm/pii_guard.py`` and
``evidence/pii_detector.py`` idioms but is intentionally SELF-CONTAINED so
the extractors and their unit tests run with zero engine/SDK deps):

* Deterministic — same input yields byte-identical output (quotes feed
  into deterministic Atom hashes downstream).
* Fail-safe — an unrecognised secret is better left unrecognised than to
  raise; scrubbing NEVER throws on well-formed ``str`` input. It errs
  toward over-redaction (false positives are annoying; a leaked key is a
  breach), but only on high-confidence patterns to keep quotes legible.
* Order-stable — high-value structural secrets (private keys, connection
  strings, credentialed assignments) are redacted before narrower token
  and PII patterns so the widest match wins.

The public surface is :func:`scrub` (redact) and :func:`find_secrets`
(detect, used by tests and by log-hygiene assertions). Neither ever
returns or logs the original secret text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Pattern, Tuple

__all__ = ["SecretHit", "scrub", "find_secrets", "REDACTION_RE"]

# A redaction token the rest of the system can recognise and strip when it
# needs to check that the NON-secret remainder of a quote is verbatim.
REDACTION_RE = re.compile(r"\[REDACTED:[A-Z0-9_]+\]")


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum — cuts the credit-card regex false-positive
    rate (an arbitrary 16-digit id is not a card number)."""
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if not 0 <= n <= 9:
            return False
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ── Structural secrets (redact whole span) ───────────────────────────────
# Each entry: (name, compiled pattern, placeholder). Applied in order.
_SPAN_PATTERNS: List[Tuple[str, Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
            r"[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
        ),
        "[REDACTED:PRIVATE_KEY]",
    ),
    ("aws_access_key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), "[REDACTED:AWS_KEY]"),
    ("github_token", re.compile(r"(?<![A-Za-z0-9_])gh[posru]_[A-Za-z0-9]{20,}(?![A-Za-z0-9_])"), "[REDACTED:GITHUB_TOKEN]"),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9])"), "[REDACTED:SLACK_TOKEN]"),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_\-]{35}(?![A-Za-z0-9_-])"), "[REDACTED:GOOGLE_API_KEY]"),
    ("stripe_key", re.compile(r"(?<![A-Za-z0-9_])[rs]k_(?:live|test)_[0-9A-Za-z]{16,}(?![A-Za-z0-9_])"), "[REDACTED:STRIPE_KEY]"),
    # OpenAI / generic "sk-" secret keys (the most common leaked key in modern
    # source): sk-… and sk-proj-… followed by >=20 base62 chars.
    ("openai_key", re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9]{20,}(?![A-Za-z0-9])"), "[REDACTED:OPENAI_KEY]"),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}(?![A-Za-z0-9_-])"), "[REDACTED:JWT]"),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"), "[REDACTED:BEARER]"),
]

# ── Credentialed assignments / URIs (redact only the SECRET sub-group) ────
# Keys whose assigned literal is a secret: password = "...", apiKey: '...'.
_SECRET_KEY_ASSIGN = re.compile(
    r"(?i)(?P<key>\b(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|client[_-]?secret|auth[_-]?token|private[_-]?key)\b)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<q>['\"])(?P<val>(?:(?!(?P=q)).){3,})(?P=q)"
)
# URI userinfo with a password: scheme://user:PASSWORD@host
_URI_CRED = re.compile(
    r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:@/]+:)(?P<pw>[^\s:@/]{1,})(?P<at>@)"
)

# ── Narrow PII (redact whole span) ────────────────────────────────────────
_PII_PATTERNS: List[Tuple[str, Pattern[str], str]] = [
    ("email", re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[REDACTED:EMAIL]"),
    ("us_ssn", re.compile(r"(?<!\d)(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)"), "[REDACTED:SSN]"),
    ("iban", re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}[A-Z0-9]{11,30}(?![A-Z0-9])"), "[REDACTED:IBAN]"),
    ("phone_e164", re.compile(r"(?<!\d)\+\d[\d\s\-().]{6,18}\d(?!\d)"), "[REDACTED:PHONE]"),
]

_CC_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")


@dataclass(frozen=True)
class SecretHit:
    """One detected secret. ``sample`` is ALWAYS masked — the raw secret
    is never stored on the hit so a hit can be safely logged."""

    kind: str
    span: Tuple[int, int]
    sample: str  # masked ("ab***yz"), never the raw secret


def _mask_sample(raw: str) -> str:
    """Return a length-preserving-ish masked hint safe to log."""
    if len(raw) <= 4:
        return "***"
    return f"{raw[:2]}***{raw[-2:]}"


def find_secrets(text: str) -> List[SecretHit]:
    """Return every secret/PII detection in ``text`` (masked samples only).

    Used by tests (secret-scrub proof) and by log-hygiene checks. Never
    raises on ``str`` input; non-str returns ``[]``.
    """
    if not isinstance(text, str) or not text:
        return []
    hits: List[SecretHit] = []
    for name, pat, _ in _SPAN_PATTERNS:
        for m in pat.finditer(text):
            hits.append(SecretHit(name, (m.start(), m.end()), _mask_sample(m.group(0))))
    for m in _SECRET_KEY_ASSIGN.finditer(text):
        s, e = m.span("val")
        hits.append(SecretHit("assigned_secret", (s, e), _mask_sample(m.group("val"))))
    for m in _URI_CRED.finditer(text):
        s, e = m.span("pw")
        hits.append(SecretHit("uri_password", (s, e), _mask_sample(m.group("pw"))))
    for name, pat, _ in _PII_PATTERNS:
        for m in pat.finditer(text):
            hits.append(SecretHit(name, (m.start(), m.end()), _mask_sample(m.group(0))))
    for m in _CC_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if _luhn_ok(digits):
            hits.append(SecretHit("credit_card", (m.start(), m.end()), _mask_sample(m.group(0))))
    hits.sort(key=lambda h: h.span)
    return hits


def scrub(text: str) -> str:
    """Return ``text`` with every recognised secret/PII replaced by a
    ``[REDACTED:KIND]`` token.

    Deterministic and fail-safe: never raises on ``str`` input; ``None``
    or non-str yields ``""``. Structural secrets and credentialed
    assignments are redacted before narrow token/PII patterns so the
    widest, highest-confidence match wins.
    """
    if not isinstance(text, str) or not text:
        return "" if text in (None, "") else str(text)

    out = text
    # 1) Whole-span structural secrets (private keys, cloud tokens, JWTs…).
    for _name, pat, placeholder in _SPAN_PATTERNS:
        out = pat.sub(placeholder, out)

    # 2) Credentialed assignments — keep the key + operator, redact the value.
    out = _SECRET_KEY_ASSIGN.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{m.group('q')}[REDACTED:SECRET]{m.group('q')}",
        out,
    )
    # 3) URI userinfo passwords — keep scheme+user, redact the password.
    out = _URI_CRED.sub(lambda m: f"{m.group('pre')}[REDACTED:PASSWORD]{m.group('at')}", out)

    # 4) Narrow PII.
    for _name, pat, placeholder in _PII_PATTERNS:
        out = pat.sub(placeholder, out)

    # 5) Luhn-validated credit cards (guarded so we don't nuke every long id).
    def _cc_sub(m: "re.Match[str]") -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[REDACTED:CC]" if _luhn_ok(digits) else m.group(0)

    out = _CC_CANDIDATE.sub(_cc_sub, out)
    return out


def scrubber() -> Callable[[str], str]:
    """Return the :func:`scrub` callable (dependency-injection convenience
    so extractors can accept a ``scrub`` parameter and default to this)."""
    return scrub

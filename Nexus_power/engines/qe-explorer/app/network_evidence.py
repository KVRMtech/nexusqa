"""M2.5 — network capture as STRUCTURED, CORRELATED, AUDITABLE evidence.

Pure + stdlib-only.  Everything here is a function over already-captured data:
the Playwright adapter owns capture, this module owns *what a captured call is
allowed to say* — which headers survive, how a body is described without
carrying its contents, how a raw URL becomes an application-level path
template, and how the stream is shaped into the entries the network oracle
already knows how to read.

Three rules run through all of it:

1. **A repeated call is not a duplicate.**  Three retries are three events.  The
   ordinal (``sequence``) is assigned at capture and never re-derived, so
   ordering survives any downstream re-sort, dedup or transport that does not
   preserve list order.

2. **Redaction is by allow-list, never by blocklist.**  A header is dropped
   unless it is named here; a header whose *value* is a credential is recorded
   as a NAMED PRESENCE (``authorization: <bearer>``) and never as its value.
   Bodies are described by shape — byte count, media type, and for structured
   bodies the KEY NAMES only — never by content.  This extends the inherited
   posture (query strings dropped, paths PII-scrubbed) rather than sitting
   beside it.

3. **What is not known is not invented.**  A response body cannot be read from
   the synchronous ``response`` listener without awaiting, and that listener is
   deliberately sync (M1.5: an awaiting listener races the action that produced
   it).  So ``response_shape`` is derived from the media type and says so via
   ``shape_source``.  It never claims to have parsed a body it never read.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl

from . import emit

# ─── Header allow-list ───────────────────────────────────────────────────────

#: Headers whose VALUE is evidence about the API contract and carries no user
#: data.  Anything not named here is dropped, so a header a future app invents
#: cannot leak by default.
_VALUE_SAFE_HEADERS = frozenset({
    "content-type", "content-length", "accept", "accept-encoding",
    "cache-control", "etag", "if-none-match", "retry-after", "location",
    "x-request-id", "x-correlation-id", "x-trace-id", "x-ratelimit-limit",
    "x-ratelimit-remaining", "x-ratelimit-reset", "x-api-version",
    "access-control-allow-origin", "vary", "server-timing",
})

#: Headers whose PRESENCE is evidence (this endpoint is authenticated) and whose
#: VALUE is a credential.  Recorded as ``<scheme>`` / ``<present>``, never as the
#: value itself.
_PRESENCE_ONLY_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "x-auth-token", "x-access-token", "x-csrf-token",
    "x-xsrf-token", "authentication", "x-session-id",
})

_MAX_HEADERS = 24
_MAX_HEADER_VALUE = 200


def _auth_presence(name: str, value: str) -> str:
    """Describe a credential-bearing header WITHOUT its value.

    An ``Authorization: Bearer eyJ...`` becomes ``<bearer>``: the scheme is the
    evidence (it tells the catalog how this endpoint is authenticated), the
    token is the secret.  An unrecognised scheme degrades to ``<present>``
    rather than falling through to the raw value.
    """
    if name in ("authorization", "proxy-authorization", "authentication"):
        scheme = (value or "").strip().split(" ", 1)[0].lower()
        if scheme in ("bearer", "basic", "digest", "negotiate", "ntlm", "hmac"):
            return "<" + scheme + ">"
        return "<present>" if value else ""
    return "<present>" if value else ""


def redact_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Allow-listed, value-redacted view of one header dict.

    Deterministically ordered (sorted) so the same request produces the same
    evidence on every run — a golden that reordered per-run would be untestable.
    """
    out: dict[str, str] = {}
    for raw_name, raw_value in sorted((headers or {}).items()):
        name = str(raw_name or "").strip().lower()
        value = str(raw_value or "")
        if name in _PRESENCE_ONLY_HEADERS:
            marker = _auth_presence(name, value)
            if marker:
                out[name] = marker
        elif name in _VALUE_SAFE_HEADERS:
            # Value-safe by NAME, still scrubbed by CONTENT: `location` and
            # `etag` are the two that routinely carry an identifier.
            out[name] = emit.scrub_value(value[:_MAX_HEADER_VALUE]).value
        if len(out) >= _MAX_HEADERS:
            break
    return out


# ─── Auth pattern ────────────────────────────────────────────────────────────

def auth_pattern(request_headers: Mapping[str, str] | None) -> str:
    """How this call authenticated, from the headers alone.

    Ordered most-specific-first so an endpoint sending both a bearer token and a
    session cookie is reported as ``bearer`` (what it actually authenticates
    with) rather than ``cookie`` (what the browser sends everywhere).  Works on
    RAW or already-redacted headers: the presence marker ``<bearer>`` and the
    real value ``Bearer eyJ...`` both resolve to ``bearer``.
    """
    h = {str(k).lower(): str(v) for k, v in (request_headers or {}).items()}
    auth = h.get("authorization", "")
    if auth:
        low = auth.lower()
        if "bearer" in low:
            return "bearer"
        if "basic" in low:
            return "basic"
        return "authorization"
    for key in ("x-api-key", "api-key", "x-auth-token", "x-access-token"):
        if h.get(key):
            return "api_key"
    if h.get("cookie"):
        return "cookie"
    return "none"


# ─── Body description (shape, never content) ─────────────────────────────────

_STRUCTURED_KEY_LIMIT = 40

#: Key names that ARE the secret, not merely a field carrying one.  A key name is
#: normally safe to record (it describes the API contract); these are the
#: exceptions, because the name plus the fact a value was sent is the credential.
_SECRET_KEY_RE = re.compile(
    r"password|passwd|secret|token|api[_-]?key|credential|ssn|authorization|"
    r"card[_-]?number|cvv|pin\b|private[_-]?key",
    re.I,
)

_MULTIPART_NAME_RE = re.compile(r'name="([^"]{1,80})"')


def describe_body(post_data: str | None, mime: str) -> dict[str, Any]:
    """Describe a REQUEST body: byte size, media type, and KEY NAMES only.

    Never the values.  A JSON or form body's key names are the API contract and
    are what makes an endpoint inventory useful ("POST /quote takes age, state,
    coverage"); the values are the user's data and are exactly what must not
    ride into a catalog.  Key names that are themselves secrets are masked.

    Best-effort by construction: an unparseable or binary body still yields an
    honest size + media type, never an exception and never a guess at content.
    """
    text = post_data or ""
    desc: dict[str, Any] = {
        "bytes": len(text.encode("utf-8", "replace")) if text else 0,
        "mime": (mime or "").split(";", 1)[0].strip().lower(),
        "keys": [],
        "keys_source": "none",
    }
    if not text:
        return desc

    keys: list[str] = []
    source = "none"
    mime_type = desc["mime"]
    try:
        if "json" in mime_type:
            import json
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                keys, source = [str(k) for k in parsed.keys()], "json"
            elif isinstance(parsed, list):
                # A list body's shape is its first object's keys, not its length.
                first = parsed[0] if parsed else None
                if isinstance(first, dict):
                    keys, source = [str(k) for k in first.keys()], "json_array"
                else:
                    source = "json_array"
        elif "x-www-form-urlencoded" in mime_type:
            keys = [str(k) for k, _ in parse_qsl(text, keep_blank_values=True)]
            source = "form"
        elif "multipart/form-data" in mime_type:
            keys = list(_MULTIPART_NAME_RE.findall(text))
            source = "multipart"
    except Exception:
        # An unparseable body is described by size alone — honestly.
        keys, source = [], "unparsed"

    seen: set[str] = set()
    masked: list[str] = []
    for key in keys:
        key = key.strip()[:80]
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        masked.append("<secret>" if _SECRET_KEY_RE.search(key) else key)
        if len(masked) >= _STRUCTURED_KEY_LIMIT:
            break
    desc["keys"] = masked
    desc["keys_source"] = source
    return desc


# ─── Response shape ──────────────────────────────────────────────────────────

def response_shape(mime: str, byte_len: Any) -> str:
    """The response's shape, from its MEDIA TYPE.

    Deliberately NOT a parse: the ``response`` listener is synchronous and
    reading a body requires an await, which would race the action that produced
    the response.  Callers are told which is which by ``shape_source`` on the
    event, so nothing downstream can mistake a media-type inference for a body
    that was read.
    """
    mime_type = (mime or "").split(";", 1)[0].strip().lower()
    try:
        size = int(byte_len)
    except (TypeError, ValueError):
        size = -1
    if size == 0:
        return "empty"
    if not mime_type:
        return "unknown"
    if mime_type == "text/event-stream":
        return "sse"
    if "json" in mime_type:
        return "json"
    if mime_type.endswith("/xml") or mime_type.endswith("+xml"):
        return "xml"
    if mime_type == "text/html":
        return "html"
    if mime_type.startswith("text/"):
        return "text"
    return "binary"


# ─── Path templating ─────────────────────────────────────────────────────────

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
_DIGITS_RE = re.compile(r"^\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: A digit-led segment with separators — a policy number, an SSN, a phone, an
#: account reference.  Caught explicitly because none of the patterns above see
#: it: it is not all digits (the dashes), not long enough to be "opaque", and not
#: shaped like a date.  Found by the redaction test, not by inspection, and it is
#: the shape most likely to be personal data.
_NUMERICISH_RE = re.compile(r"^\d[\d\-_.]{2,}$")
#: A long blob that CONTAINS a digit is an opaque id, not a route word.  Requiring
#: a digit keeps genuine route words ("underwriting", "policy-administration")
#: out of the template.
_OPAQUE_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9_\-]{12,}$")
#: Already-redacted spans must not be re-templated into `{id}` — losing the fact
#: that a PII class was detected there would hide a redaction from an auditor.
_REDACTED_RE = re.compile(r"^\[REDACTED:[^\]]+\]$")

_MAX_SEGMENTS = 12


def path_template(path: str) -> str:
    """Normalize one URL path into an application-level route template.

    ``/api/policies/8837/documents/3f2b...`` -> ``/api/policies/{id}/documents/{hex}``

    This is what makes the inventory an *application* API surface rather than a
    list of the particular records the crawl happened to touch: two crawls of
    the same app produce the same templates even though they touched different
    ids.  It is also a second redaction layer — an identifier that survived
    scrubbing does not survive templating.
    """
    raw = str(path or "")
    if not raw.startswith("/"):
        raw = "/" + raw
    segments = raw.split("/")
    out: list[str] = []
    for seg in segments[:_MAX_SEGMENTS + 1]:
        if not seg:
            out.append(seg)
        elif _REDACTED_RE.match(seg):
            out.append(seg)
        elif _DIGITS_RE.match(seg):
            out.append("{id}")
        elif _UUID_RE.match(seg):
            out.append("{uuid}")
        elif _DATE_RE.match(seg):
            out.append("{date}")
        elif _NUMERICISH_RE.match(seg):
            out.append("{id}")
        elif _HEX_RE.match(seg):
            out.append("{hex}")
        elif _OPAQUE_RE.match(seg):
            out.append("{token}")
        else:
            out.append(seg[:80])
    template = "/".join(out) or "/"
    if len(segments) > _MAX_SEGMENTS + 1:
        template += "/..."
    return template


# ─── The oracle adapter (T-NET-05) ───────────────────────────────────────────

def _truthy(value: Any) -> bool:
    """Interpret a flag that may have crossed a STRING-typed transport.

    The manifest field is typed ``dict[str, str]``, so a captured ``failed:
    False`` is re-read as the string ``"false"`` — and ``bool("false")`` is
    True.  A plain truth test therefore marked every successful call, and every
    5xx, as a connection failure the moment the evidence had been through the
    manifest.  Found by reading the adapter output for a real 500.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


#: The exact keys ``network_oracle.classify_network_signal`` reads.  Named here
#: so a change on either side breaks a test rather than silently disabling the
#: oracle — which is precisely how the baseline mismatch (``start_ms`` vs
#: ``timestamp_ms``) went unnoticed.
ORACLE_ENTRY_KEYS = ("url", "method", "status", "start_ms", "failed", "error")


def to_oracle_entries(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Adapt captured network events into the network oracle's entry schema.

    The oracle windows on ``start_ms`` and reads an INT ``status``; the crawler
    records ``timestamp_ms`` and a string status.  That mismatch is why the
    oracle's structured path was dead code for crawl evidence: a missing
    ``start_ms`` does not raise, it silently disables the step window, and a
    missing producer meant the path was never reached at all.

    This is the producer.  ``timestamp_ms`` is carried through as well as mapped,
    so an entry stays joinable to the visit/step evidence it came from after the
    oracle has read it, and the correlation fields ride along so a fired oracle
    can name the click that caused the failing request.
    """
    out: list[dict[str, Any]] = []
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        try:
            status = int(str(event.get("status") or "0").strip() or 0)
        except (TypeError, ValueError):
            status = 0
        raw_ts = event.get("timestamp_ms", event.get("start_ms"))
        try:
            timestamp = int(raw_ts)
        except (TypeError, ValueError):
            timestamp = None
        failed = _truthy(event.get("failed")) or (status == 0 and bool(event.get("error")))
        out.append({
            "url": str(event.get("url") or ""),
            "method": str(event.get("method") or "").upper(),
            "status": status,
            "start_ms": timestamp,
            "end_ms": timestamp,
            "timestamp_ms": timestamp,
            "failed": failed,
            "error": str(event.get("error") or ""),
            "sequence": event.get("sequence"),
            "action_token": str(event.get("action_token") or ""),
            "action_label": str(event.get("action_label") or ""),
            "action_verb": str(event.get("action_verb") or ""),
        })
    return out


def observed_server_errors(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every event whose OBSERVED status is 5xx — structurally, not by string search.

    The oracle's text fallback exists because the runner had no structured
    entries.  A crawl does, so a 5xx here is a read of an integer, never a regex
    over an error message.
    """
    out: list[dict[str, Any]] = []
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        try:
            status = int(str(event.get("status") or "0").strip() or 0)
        except (TypeError, ValueError):
            continue
        if 500 <= status <= 599:
            out.append(dict(event))
    return out


__all__ = [
    "ORACLE_ENTRY_KEYS", "auth_pattern", "describe_body", "observed_server_errors",
    "path_template", "redact_headers", "response_shape", "to_oracle_entries",
]

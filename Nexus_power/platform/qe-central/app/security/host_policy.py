"""QE-Central M0.5 — the egress host-allowlist WRITE-BOUNDARY policy (T-SEC-04).

WHY THIS EXISTS
===============
``client_apps.fences.allowed_hosts`` (and ``fences.idp_domains``) is
TENANT-CONTROLLED data that becomes the squid egress allowlist verbatim
(``routers/explorations._allowlist_domains`` → ``_write_egress_allowlist``).
Squid treats a leading-dot entry as "this domain and every subdomain", so a
tenant registering ``[".com"]`` turned the fenced browser into an unrestricted
SSRF proxy for the whole ``.com`` namespace — and nothing rejected it, because
validation only ever happened (partially) at crawl time.

This module is the SINGLE normalise-then-validate gate.  It runs at the WRITE
boundary (``routers/apps`` create/update for both the app and its Environment
Profiles) so a dangerous value can never be PERSISTED, and again at dispatch
(defence in depth) so a row written before this gate existed still cannot reach
squid.

DOCTRINE
========
  * NORMALISE FIRST, VALIDATE SECOND.  Percent-decoding, unicode/IDNA folding,
    case folding, trailing dots, surrounding brackets and zone ids are all
    resolved BEFORE any check runs, so ``%2Elocalhost``, ``LOCALHOST.``,
    ``ⓛocalhost`` and ``[::1]`` cannot walk past a check written for the plain
    form.
  * FAIL CLOSED.  Anything we cannot confidently parse is REJECTED, never
    passed through "just in case".
  * NO IP LITERALS AT ALL.  A squid dstdomain allowlist is a DOMAIN allowlist;
    an IP literal in it is either useless or an attempt to reach infrastructure
    by address.  Every IPv4/IPv6 literal (and every encoded/mapped form of one)
    is refused, which covers 169.254.169.254, ::1, fd00::/8, 127.0.0.1,
    0x7f.1, 2130706433 and ::ffff:169.254.169.254 in one rule.
  * PUBLIC SUFFIXES ARE NEVER A FENCE.  ``.com``/``*.com``/``co.uk`` name a
    whole registry, not an application.  Rejected.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import unquote

logger = logging.getLogger(__name__)

#: Hard maximum on a single host entry (DNS limit) and on the whole list, so a
#: pathological registration cannot blow up the squid config file.
MAX_HOST_LENGTH = 253
MAX_HOSTS = 64

#: Host labels that always name infrastructure rather than a client application.
_BLOCKED_EXACT = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data", "instance-data.ec2.internal",
    "kubernetes", "kubernetes.default", "kubernetes.default.svc",
    # This fleet's own control plane — a crawl must never egress back into it.
    "qe-central", "qe-explorer", "platform-api", "postgres", "redis",
    "qec-egress-proxy",
})

#: Suffixes that always name a private/internal namespace.
_BLOCKED_SUFFIXES = (
    ".internal", ".local", ".localdomain", ".localhost", ".intranet",
    ".corp", ".home", ".lan", ".private", ".onion", ".invalid",
    ".svc", ".svc.cluster.local", ".cluster.local",
)

#: Public suffixes / registry-level names.  An allowlist entry that resolves to
#: one of these (with or without a leading dot or wildcard) fences NOTHING — it
#: authorises an entire registry.  This is not the full PSL (the container has
#: no egress to refresh one); it is the conservative set that makes the obvious
#: catastrophic forms (".com", "*.com", "co.uk") impossible, backed by the
#: structural minimum-label rule below which catches the long tail.
_PUBLIC_SUFFIXES = frozenset({
    # generic
    "com", "net", "org", "edu", "gov", "mil", "int", "info", "biz", "name",
    "pro", "io", "co", "ai", "app", "dev", "cloud", "xyz", "online", "site",
    "shop", "store", "tech", "live", "life", "world", "today", "email",
    # country-code
    "us", "uk", "ca", "au", "nz", "de", "fr", "es", "it", "nl", "be", "ch",
    "at", "se", "no", "dk", "fi", "ie", "pt", "pl", "cz", "ru", "cn", "jp",
    "kr", "in", "br", "mx", "za", "sg", "hk", "tw", "il", "ae", "tr",
    # common two-level public suffixes
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "co.kr", "or.kr", "ne.kr", "go.kr",
    "co.in", "net.in", "org.in", "gov.in",
    "co.za", "org.za", "net.za", "gov.za",
    "com.br", "net.br", "org.br", "gov.br",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "com.mx", "com.sg", "com.hk", "com.tw", "com.tr", "co.il",
    # multi-tenant hosting suffixes: one entry there is every other customer
    "appspot.com", "herokuapp.com", "azurewebsites.net", "cloudfront.net",
    "amazonaws.com", "s3.amazonaws.com", "elb.amazonaws.com", "github.io",
    "netlify.app", "vercel.app", "pages.dev", "workers.dev", "web.app",
    "firebaseapp.com", "blob.core.windows.net", "sslip.io", "nip.io",
})

#: A single DNS label: letters/digits/hyphen, not starting or ending with '-'.
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: Zero-width / format / NUL characters that must never survive normalisation
#: into a host we then compare against an ASCII blocklist — ``local​host``
#: and ``localhost\x00.evil.example`` must both fold to ``localhost``.
#: Spelled as CODEPOINTS on purpose: these are invisible in a source file, and a
#: literal NUL in a string is not even loadable by CPython.
_ZERO_WIDTH = dict.fromkeys(
    (
        0x0000,  # NUL — truncation tricks in downstream C string handling
        0x00AD,  # soft hyphen
        0x200B, 0x200C, 0x200D, 0x200E, 0x200F,  # ZWSP/ZWNJ/ZWJ/LRM/RLM
        0x2060,  # word joiner
        0xFEFF,  # BOM / zero-width no-break space
    ),
    None,
)


class HostPolicyError(ValueError):
    """A rejected allowlist entry.  ``entry`` + ``reason`` are safe to surface."""

    def __init__(self, entry: str, reason: str) -> None:
        self.entry = str(entry)[:120]
        self.reason = str(reason)
        super().__init__(f"{self.entry!r} rejected: {self.reason}")


def _decode(raw: str) -> str:
    """Percent-decode repeatedly, strip zero-width/format chars, casefold.

    Repeated decoding closes the double-encoding bypass (``%252E`` → ``%2E`` →
    ``.``); it is bounded so a decode bomb cannot spin.
    """
    text = str(raw or "")
    for _ in range(4):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text.translate(_ZERO_WIDTH).strip().casefold()


def _strip_scheme_and_path(raw: str) -> str:
    """Reduce an entry to the bare host, REFUSING the ambiguous shapes.

    An egress allowlist takes HOSTS.  A URL, a path, or userinfo in an entry is
    an operator who believes they are scoping something squid will not scope —
    ``acme.example/admin`` allowlists the whole of ``acme.example`` and silently
    discards the part they cared about.  Refusing is the honest answer; the port
    and IPv6 brackets are stripped because those are unambiguous.
    """
    text = raw
    if "/" in text or "\\" in text:
        raise HostPolicyError(
            raw,
            "an allowlist entry is a HOST, not a URL or a path — squid fences by "
            "domain and would ignore everything after the host, so this would "
            "allow more than it appears to",
        )
    if "?" in text or "#" in text:
        raise HostPolicyError(raw, "query/fragment is not part of a host")
    if "@" in text:
        raise HostPolicyError(raw, "userinfo is not part of a host")
    # bracketed IPv6 (keep the brackets off; the literal check catches it)
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    # ":port" — only when the remainder is numeric, so an unbracketed IPv6
    # literal ("fe80::1") is not silently truncated into something that parses
    # as a hostname.
    if ":" in text:
        head, _, tail = text.rpartition(":")
        if head and tail.isdigit():
            text = head
    return text


def _is_ip_literal(host: str) -> bool:
    """True when ``host`` is any IP literal, in any representation.

    Covers dotted-quad, bare-integer, hex/octal ``inet_aton`` forms, IPv6, and
    IPv4-mapped IPv6 — every shape that would let an allowlist entry name an
    address (metadata service, loopback, an internal subnet) instead of a
    domain.
    """
    candidate = host.split("%", 1)[0]  # drop an IPv6 zone id
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass
    # inet_aton-style: 2130706433, 0x7f000001, 017700000001, 127.1, 0x7f.1
    compact = candidate.replace(".", "")
    if compact and all(c in "0123456789abcdefx" for c in compact):
        try:
            int(candidate, 0)
            return True
        except ValueError:
            pass
        parts = candidate.split(".")
        if 1 < len(parts) <= 4:
            try:
                for part in parts:
                    int(part, 0)
                return True
            except ValueError:
                pass
    return False


def _registrable_shape(labels: list[str], *, wildcard: bool) -> tuple[bool, str]:
    """Is this label sequence specific enough to be a fence?

    Two distinct rules, because the two entry shapes carry different blast radii:

      * a PUBLIC SUFFIX is never acceptable in either shape — ``com`` /
        ``co.uk`` / ``herokuapp.com`` name a registry or a multi-tenant host,
        so an entry there authorises every other customer on it;
      * a WILDCARD (``.acme.example`` — squid's domain+subdomains form) must
        additionally carry at least TWO labels.  ``.com`` is a single label and
        would open the whole namespace; an EXACT single-label entry
        (``acme-life``, a container/service name on an internal network)
        authorises exactly one hostname and stays legal.
    """
    joined = ".".join(labels)
    if joined in _PUBLIC_SUFFIXES:
        return False, (
            f"'{joined}' is a public suffix or multi-tenant hosting domain (a "
            "whole registry, not an application host) — an allowlist entry here "
            "fences nothing"
        )
    if wildcard and len(labels) < 2:
        return False, (
            f"'*.{joined}' is an unrestricted wildcard over a single label — a "
            "subdomain wildcard must name at least a registrable domain "
            "(e.g. '*.acme.example')"
        )
    return True, ""


def normalize_host_entry(raw: str) -> str:
    """Normalise ONE allowlist entry to its canonical comparable form.

    Returns the entry with any leading ``*.``/``.`` wildcard marker preserved as
    a single leading dot (squid's ``dstdomain`` subdomain form), IDNA-encoded,
    lowercased, trailing dot removed.  Raises :class:`HostPolicyError` when the
    input cannot be reduced to a host at all.
    """
    text = _decode(raw)
    if not text:
        raise HostPolicyError(raw, "empty entry")
    if "://" in text:
        raise HostPolicyError(
            raw, "an allowlist entry is a bare host, not a URL (drop the scheme)",
        )
    text = _strip_scheme_and_path(text)
    if not text:
        raise HostPolicyError(raw, "no host in entry")

    wildcard = False
    while text.startswith("*."):
        wildcard = True
        text = text[2:]
    if text.startswith("."):
        wildcard = True
        text = text.lstrip(".")
    if text.endswith("."):
        text = text.rstrip(".")
    if not text:
        raise HostPolicyError(raw, "entry is a bare wildcard — it allows every host")

    # IDNA/punycode: fold unicode to its ASCII form BEFORE any comparison, so a
    # homoglyph host cannot dodge the blocklists.
    if any(ord(ch) > 127 for ch in text):
        try:
            text = text.encode("idna").decode("ascii").lower()
        except Exception as exc:
            raise HostPolicyError(raw, f"not a valid international hostname ({exc})")

    return ("." + text) if wildcard else text


def validate_host_entry(raw: str) -> str:
    """Normalise + fully validate ONE allowlist entry; return its canonical form.

    Raises :class:`HostPolicyError` for every dangerous or malformed shape:
    bare/overbroad wildcards, public suffixes, IP literals (all encodings),
    metadata/link-local/private targets, internal namespaces, CIDR ranges, and
    malformed hostnames.
    """
    entry = normalize_host_entry(raw)
    wildcard = entry.startswith(".")
    host = entry.lstrip(".")

    if "/" in host or "\\" in host:
        raise HostPolicyError(raw, "CIDR ranges and paths are not host allowlist entries")
    if len(host) > MAX_HOST_LENGTH:
        raise HostPolicyError(raw, f"host exceeds {MAX_HOST_LENGTH} characters")
    if "*" in host or "?" in host:
        raise HostPolicyError(
            raw,
            "only a leading '*.' subdomain wildcard is supported — an embedded "
            "wildcard cannot be fenced",
        )
    if _is_ip_literal(host):
        raise HostPolicyError(
            raw,
            "IP literals are never allowlist entries (an egress fence is a "
            "DOMAIN allowlist; an address here names infrastructure, not an app)",
        )
    if host in _BLOCKED_EXACT:
        raise HostPolicyError(raw, f"'{host}' names internal infrastructure")
    for suffix in _BLOCKED_SUFFIXES:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            raise HostPolicyError(
                raw, f"'{suffix}' is a private/internal namespace, never a crawl target",
            )

    labels = host.split(".")
    for label in labels:
        if not _LABEL_RE.match(label):
            raise HostPolicyError(
                raw, f"'{label}' is not a valid DNS label",
            )
    # THE CATCH-ALL FOR NUMERIC-FORM ADDRESSES.  No delegated TLD is numeric, so
    # a final label with no letter in it means this is an address wearing a
    # hostname's punctuation — ``0177.0.0.1`` (octal), ``127.1`` (short form),
    # ``2130706433`` (integer).  Enumerating those encodings one at a time is a
    # losing game; requiring the last label to look like a real TLD is not.
    if not any(ch.isalpha() for ch in labels[-1]):
        raise HostPolicyError(
            raw,
            f"'{host}' ends in a non-alphabetic label — that is an IP address "
            "in one of its numeric encodings, not a domain name",
        )
    ok, reason = _registrable_shape(labels, wildcard=wildcard)
    if not ok:
        raise HostPolicyError(raw, reason)
    return entry


def validate_allowed_hosts(
    hosts, *, field: str = "fences.allowed_hosts",
) -> list[str]:
    """Validate a whole allowlist; return the normalised, de-duplicated list.

    Raises :class:`HostPolicyError` on the FIRST offending entry — a partially
    accepted allowlist is not a fence.  An empty/absent list is returned as
    ``[]`` (the caller decides whether an empty fence is legal; dispatch already
    refuses one).
    """
    if hosts is None:
        return []
    if isinstance(hosts, (str, bytes)):
        raise HostPolicyError(str(hosts), f"{field} must be a list of hosts")
    try:
        items = list(hosts)
    except TypeError:
        raise HostPolicyError(str(hosts), f"{field} must be a list of hosts")
    if len(items) > MAX_HOSTS:
        raise HostPolicyError(
            f"<{len(items)} entries>", f"{field} may hold at most {MAX_HOSTS} hosts",
        )
    out: list[str] = []
    for item in items:
        entry = validate_host_entry(item)
        if entry not in out:
            out.append(entry)
    return out


def assert_fences_hosts(fences) -> None:
    """Validate every host-bearing key of a ``fences`` dict, in place-safe form.

    Raises :class:`HostPolicyError`.  Called from the app + Environment-Profile
    write paths so a dangerous fence is never PERSISTED.
    """
    if not isinstance(fences, dict):
        return
    for key in ("allowed_hosts", "idp_domains"):
        if key in fences and fences.get(key) is not None:
            validate_allowed_hosts(fences.get(key), field=f"fences.{key}")


__all__ = [
    "HostPolicyError",
    "MAX_HOSTS",
    "MAX_HOST_LENGTH",
    "assert_fences_hosts",
    "normalize_host_entry",
    "validate_allowed_hosts",
    "validate_host_entry",
]

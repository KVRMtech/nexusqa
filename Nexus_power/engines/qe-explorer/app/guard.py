"""QE-Central Contained Explorer — the fail-closed mutation GUARD (design §3.2).

This is the most security-critical module in the product. Its single job is to
make app mutation **physically impossible** outside an attested, per-flow-approved
submit phase. Everything here is a set of PURE functions over pure data:

  * :func:`load_refuse_pack` parses + validates the versioned ``refuse_pack.yaml``
    (a malformed pack refuses to load → the explorer FAILS CLOSED and never starts);
  * :func:`classify_request` is the decision the Playwright ``context.route('**/*')``
    handler consults for every network request — a thin async wrapper calls this,
    so the entire policy is unit-testable without a browser;
  * :func:`classify_action_verb` tells the inventory which controls are
    irreversible "never-click leaves".

Doctrine (design §3.2 / STARTING_POINT.md R2):
  * fail-CLOSED everywhere — any ambiguity, unknown input, missing pack, or
    unparseable request ABORTS. Over-blocking is safe; under-blocking is
    catastrophic on real regulated data.
  * "fill-without-submit" is NOT a safety concept — containment is the method
    (the network guard), not a hope about onBlur handlers.
  * every decision carries a stable ``rule_id`` + a human ``reason`` so each
    abort is auditable and lands in a ``guard_event``.

The guard enforces the HTTP METHOD; squid (design §1.1) independently enforces
the HOST. HTTPS is CONNECT-tunnelled at the proxy, so the browser terminates TLS
and ``context.route`` still observes the real ``GET/POST/...`` — this function
therefore sees plaintext methods and urls regardless of scheme.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern:
    """Case-insensitive compile of a refuse-pack regex, memoised across calls.

    Raises :class:`re.error` on a malformed pattern — surfaced as a load-time
    ``ValueError`` by :class:`RefuseRule` so a bad pack can never load.
    """
    return re.compile(pattern, re.IGNORECASE)

# ─── HTTP method vocabulary ────────────────────────────────────────────────

#: Safe, non-mutating methods (a well-behaved server treats these as read-only).
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Methods that mutate server state — never allowed outside AUTH/SUBMIT.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Every method the guard will even reason about; anything else fails closed
#: (blocks TRACE, CONNECT, and any custom/unknown verb).
KNOWN_METHODS = READ_METHODS | MUTATING_METHODS

#: The fields a rule's regex may be tested against (design §3.2 refuse_pack).
APPLIES_TO_FIELDS = frozenset({"button_name", "url_path", "url_query"})

#: guard_event.kind vocabulary this module emits (design §3.2). The crawler
#: owns the remaining kinds (blocked_host, budget_stop, ws_blocked).
EVENT_ALLOW = ""
EVENT_BLOCKED_METHOD = "blocked_method"
EVENT_REFUSED_VERB_GET = "refused_verb_get"


class Phase(str, Enum):
    """The crawl state machine's phase (design §3.2: explore|auth|submit,
    extended by M1.3 with ``walk``).

    ``WALK`` is NOT a wider EXPLORE.  It is a separate, narrower phase the
    crawler may only be in while a platform-attested actuation window is open
    (see :mod:`app.walk_persist`), and it refuses things SUBMIT permits — an
    irreversible verb crosses in SUBMIT under a human approval, and never
    crosses in WALK, which has no human in the loop.
    """

    EXPLORE = "explore"
    AUTH = "auth"
    SUBMIT = "submit"
    WALK = "walk"


def _coerce_phase(phase: "Phase | str | None") -> "Phase | None":
    """Normalise a phase input to a :class:`Phase`, or ``None`` if unknown.

    An unknown phase is NOT an error the caller must handle here — it is a
    fail-closed ABORT inside :func:`classify_request` (unknown intent → refuse).
    """
    if isinstance(phase, Phase):
        return phase
    try:
        return Phase(str(phase).strip().lower())
    except (ValueError, AttributeError):
        return None


# ─── Guard rule ids (this module's own decisions) ──────────────────────────
# Distinct from the refuse_pack rule ids (rp.*). A guard_event.rule_id is
# either one of these ``guard.*`` ids or an ``rp.*`` id when a pack rule fired.

class GuardRule:
    """Stable rule-id constants for the guard's own (non-pack) decisions."""

    MALFORMED = "guard.malformed_request"
    UNKNOWN_METHOD = "guard.unknown_method"
    UNKNOWN_PHASE = "guard.unknown_phase"
    NO_REFUSE_PACK = "guard.no_refuse_pack"

    EXPLORE_READ_OK = "guard.explore.read_ok"
    EXPLORE_MUTATION_BLOCKED = "guard.explore.mutation_blocked"

    AUTH_READ_OK = "guard.auth.read_ok"
    AUTH_LOGIN_POST_OK = "guard.auth.login_post_ok"
    AUTH_OFFDOMAIN_POST_BLOCKED = "guard.auth.offdomain_post_blocked"
    AUTH_NONLOGIN_MUTATION_BLOCKED = "guard.auth.nonlogin_mutation_blocked"

    SUBMIT_READ_OK = "guard.submit.read_ok"
    SUBMIT_MUTATION_OK = "guard.submit.mutation_ok"
    SUBMIT_NO_ATTESTATION = "guard.submit.no_attestation"
    SUBMIT_NOT_APPROVED = "guard.submit.not_approved"

    # M1.3 — controlled walk persistence.  Every one of these except
    # WALK_READ_OK / WALK_MUTATION_OK is a refusal, and WALK_MUTATION_OK is
    # reachable ONLY behind a verified platform attestation.
    WALK_READ_OK = "guard.walk.read_ok"
    WALK_MUTATION_OK = "guard.walk.mutation_ok"
    WALK_NO_ATTESTATION = "guard.walk.no_attestation"
    WALK_IRREVERSIBLE_BLOCKED = "guard.walk.irreversible_blocked"
    WALK_NOT_AUTHORIZED = "guard.walk.not_authorized"
    WALK_BUDGET_EXCEEDED = "guard.walk.budget_exceeded"


# ─── Refuse-pack data model ────────────────────────────────────────────────


class RefuseRule(BaseModel):
    """One compiled refuse-pack rule (irreversible verb / mutation-signal GET /
    allow-override).

    The regex is compiled ONCE at load time; a rule with an invalid regex or an
    out-of-vocabulary ``applies_to`` refuses to construct, so a malformed pack
    can never be loaded (fail-closed at boot).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    match: str = Field(min_length=1)
    applies_to: tuple[str, ...] = ("button_name", "url_path", "url_query")
    severity: str = "critical"
    description: str = ""

    @field_validator("applies_to", mode="before")
    @classmethod
    def _normalise_applies_to(cls, value):
        if value is None:
            return ("button_name", "url_path", "url_query")
        if isinstance(value, str):
            value = [value]
        fields = tuple(str(f).strip() for f in value)
        unknown = [f for f in fields if f not in APPLIES_TO_FIELDS]
        if unknown or not fields:
            raise ValueError(
                f"applies_to {list(fields)!r} must be a non-empty subset of "
                f"{sorted(APPLIES_TO_FIELDS)}"
            )
        return fields

    @field_validator("severity")
    @classmethod
    def _normalise_severity(cls, value: str) -> str:
        sev = (value or "").strip().lower()
        if sev not in ("critical", "high", "medium"):
            raise ValueError(
                f"severity {value!r} must be one of critical|high|medium"
            )
        return sev

    @model_validator(mode="after")
    def _validate_regex(self) -> "RefuseRule":
        try:
            _compiled(self.match)  # compile + cache; raises on a malformed regex
        except re.error as exc:  # malformed regex → refuse the whole pack
            raise ValueError(f"rule {self.id!r} has an invalid regex: {exc}") from exc
        return self

    def matches(self, fields: Mapping[str, str]) -> bool:
        """True when the compiled regex hits ANY of this rule's ``applies_to``
        fields. Empty field values never match (nothing to mutate)."""
        regex = _compiled(self.match)
        for field_name in self.applies_to:
            value = fields.get(field_name) or ""
            if value and regex.search(value):
                return True
        return False


class RefusePack(BaseModel):
    """The parsed, validated, version-stamped refuse pack.

    Immutable data consumed only by the pure guard functions. The ``version``
    is stamped into ``crawl_meta`` and every ``guard_event`` for auditability.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    irreversible_verbs: tuple[RefuseRule, ...] = ()
    mutation_signal_get_rules: tuple[RefuseRule, ...] = ()
    allow_overrides: tuple[RefuseRule, ...] = ()

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> "RefusePack":
        seen: set[str] = set()
        for rule in (*self.irreversible_verbs, *self.mutation_signal_get_rules,
                     *self.allow_overrides):
            if rule.id in seen:
                raise ValueError(f"duplicate refuse-pack rule id {rule.id!r}")
            seen.add(rule.id)
        return self


def load_refuse_pack(path: str) -> RefusePack:
    """Load + validate the versioned refuse pack from ``path``.

    Fail-CLOSED: a missing file, malformed YAML, a bad schema, an invalid regex
    or a duplicate rule id raises — the explorer must refuse to start rather
    than crawl with a degraded or absent safety policy.

    Raises:
        FileNotFoundError: the pack file does not exist.
        ValueError:        the pack is malformed (YAML, schema, regex, or ids).
    """
    import yaml  # local import: keeps the pure decision fns import-light

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError:
        logger.error("qec.guard.refuse_pack_missing", extra={"path": path})
        raise
    except yaml.YAMLError as exc:
        logger.error("qec.guard.refuse_pack_yaml_error",
                     extra={"path": path, "error": str(exc)[:300]})
        raise ValueError(f"refuse pack {path!r} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"refuse pack {path!r} must be a mapping, got {type(raw).__name__}"
        )

    try:
        pack = RefusePack.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError → honest ValueError
        logger.error("qec.guard.refuse_pack_invalid",
                     extra={"path": path, "error": str(exc)[:300]})
        raise ValueError(f"refuse pack {path!r} is invalid: {exc}") from exc

    logger.info(
        "qec.guard.refuse_pack_loaded",
        extra={"path": path, "version": pack.version,
               "irreversible_verbs": len(pack.irreversible_verbs),
               "mutation_signal_get_rules": len(pack.mutation_signal_get_rules),
               "allow_overrides": len(pack.allow_overrides)},
    )
    return pack


# ─── Attestation (the SUBMIT-phase gate, design §3.5 env_attestation) ──────

#: 2001-09-09T01:46:40Z in epoch millis.  Any "now" below this is not a
#: wall-clock reading — it is a monotonic since-start value, a zeroed clock, or
#: seconds mistaken for milliseconds.  Freshness REFUSES rather than compares
#: across clock domains (M0.5 SI-08).
_MIN_PLAUSIBLE_EPOCH_MS = 1_000_000_000_000


class Attestation(BaseModel):
    """A client env attestation gating the SUBMIT phase (design §3.5).

    Fail-closed by construction: :meth:`is_submit_capable` grants a mutating
    submit ONLY on a disposable, time-bounded, unexpired, attributed env.
    """

    model_config = ConfigDict(extra="forbid")

    attested_by: str = ""
    env_kind: str = ""            # prod | staging | disposable
    reset_procedure: str = ""
    #: EPOCH milliseconds (UTC).  ONE canonical representation for every
    #: persisted/protocol timestamp in this system — qe-central converts the
    #: stored ISO ``expires_at`` to epoch millis at dispatch, and this field is
    #: compared ONLY against a wall-clock epoch reading.
    expires_at_ms: int | None = None

    def is_submit_capable(self, now_epoch_ms: int | None = None) -> bool:
        """True only for a disposable, attributed, time-bounded, UNEXPIRED env.

        M0.5 T-SEC-08 — THE CLOCK-DOMAIN FIX.
        =====================================
        This used to be called as ``is_submit_capable(crawler.now_ms())``, and
        ``crawler.now_ms()`` is :class:`emit.MonotonicClock` — MILLISECONDS
        SINCE CRAWL START, a number in the thousands.  ``expires_at_ms`` is
        epoch milliseconds, a number near 1.7e12.  So ``now_ms <
        expires_at_ms`` was true for roughly the next fifty thousand years:
        the freshness gate could not expire an attestation, and a submit
        authorised by an attestation that lapsed months ago still crossed.

        The two clocks now cannot meet.  A monotonic clock measures DURATION
        (the auth/submit windows still use it, correctly); freshness against a
        persisted deadline is wall-clock only, read HERE rather than accepted
        from a caller.  Passing an argument at all is optional and exists for
        deterministic tests; a value that is not plausibly an epoch reading is
        REFUSED rather than compared.
        """
        if not (self.attested_by or "").strip():
            return False
        if (self.env_kind or "").strip().lower() != "disposable":
            return False
        if self.expires_at_ms is None:
            return False
        now = int(time.time() * 1000) if now_epoch_ms is None else int(now_epoch_ms)
        if now < _MIN_PLAUSIBLE_EPOCH_MS:
            # A monotonic (since-start) reading, a zeroed clock, or a caller
            # confusing the two domains. Fail CLOSED and say so — silently
            # treating it as "very early, therefore fresh" is the bug above.
            logger.error(
                "qec.guard.attestation_clock_domain_error now_ms=%d — refusing "
                "the submit (freshness needs an epoch-ms reading, not a "
                "monotonic one)", now)
            return False
        return now < int(self.expires_at_ms)


# ─── Decision + classification result types ────────────────────────────────


@dataclass(frozen=True)
class GuardDecision:
    """The verdict for one request. ``allow`` is the gate; ``reason`` +
    ``rule_id`` make it auditable; ``event_kind`` maps a block onto the
    ``guard_event`` vocabulary (empty on allow); ``severity`` ranks a block."""

    allow: bool
    reason: str
    rule_id: str
    event_kind: str = EVENT_ALLOW
    severity: str = ""


@dataclass(frozen=True)
class VerbClassification:
    """Result of :func:`classify_action_verb` — is this control a never-click,
    point-of-no-return leaf? ``severity`` lets the inventory tune its
    never-click threshold per control kind."""

    irreversible: bool
    rule_id: str = ""
    severity: str = ""
    reason: str = ""


# ─── Registrable-domain (eTLD+1) heuristic ─────────────────────────────────
# A CONSERVATIVE heuristic, NOT the full Public Suffix List (the explorer has no
# egress to fetch/refresh a PSL). It handles the common two-level public
# suffixes so a login at ``auth.example.co.uk`` and a POST to ``example.co.uk``
# resolve to the same registrable domain. Unknown/edge inputs (IPs, single
# labels, localhost) are returned unchanged. When two hosts cannot be proven to
# share a registrable domain, the AUTH gate treats them as DIFFERENT (the caller
# passes ``is_login_domain=False``) — fail-closed.

_MULTI_PART_SUFFIXES = frozenset({
    # United Kingdom
    "co.uk", "org.uk", "gov.uk", "ac.uk", "ltd.uk", "plc.uk", "me.uk",
    "net.uk", "sch.uk", "nhs.uk", "police.uk", "mod.uk",
    # Australia / New Zealand
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au", "asn.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "geek.nz",
    # Japan / Korea
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "ad.jp", "ed.jp",
    "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr",
    # India / South Africa
    "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in", "gov.in",
    "co.za", "org.za", "net.za", "gov.za", "web.za",
    # Brazil / Mexico / LatAm
    "com.br", "net.br", "org.br", "gov.br", "edu.br",
    "com.mx", "org.mx", "gob.mx", "com.ar", "com.co", "com.pe", "com.ve",
    # Asia-Pacific
    "com.sg", "com.hk", "com.cn", "com.tw", "com.my", "com.ph", "com.vn",
    "com.pk", "com.bd", "com.np", "co.th", "co.id", "or.id", "co.il",
    # Europe / Middle East
    "com.tr", "org.tr", "gov.tr", "co.at", "or.at", "com.ua", "com.ru",
    "co.ke", "co.ug", "co.tz", "com.ng", "com.eg", "com.sa", "com.qa",
    "com.kw", "com.bh", "com.lb",
})

# WILDCARD-HOST SERVICES: anyone may claim any subdomain, so the registrable
# domain says nothing about WHOSE application this is.
#
# `vkpowerlife.136-85-106-73.sslip.io` reduces to `sslip.io` under the rule above,
# which would make every unrelated application on that service share a login domain
# — and the domain is half of the reuse key that decides whether a recorded login is
# PROPOSED to another app. A false match there offers one customer's login recipe
# for another customer's application.
#
# For these, the full host IS the identity. Over-specific keying costs a missed
# reuse offer; over-general keying makes a wrong one. Only one of those is safe.
# KEEP IN SYNC with login_observation._WILDCARD_HOST_SUFFIXES.
_WILDCARD_HOST_SUFFIXES = frozenset({
    # IP-embedding wildcard DNS (dev/test estates live here)
    "sslip.io", "nip.io", "xip.io", "traefik.me", "localtest.me", "lvh.me",
    # tunnels
    "ngrok.io", "ngrok-free.app", "loca.lt", "trycloudflare.com",
    # PaaS app hosting where each customer gets a subdomain
    "herokuapp.com", "azurewebsites.net", "vercel.app", "netlify.app",
    "pages.dev", "workers.dev", "onrender.com", "fly.dev", "railway.app",
    "github.io", "gitlab.io", "cloudfront.net", "elasticbeanstalk.com",
    "appspot.com", "web.app", "firebaseapp.com", "repl.co", "glitch.me",
})



def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 for a hostname (conservative heuristic; see above).

    * strips a trailing dot, a ``:port`` suffix, and lower-cases;
    * returns IP addresses and single-label / two-label hosts unchanged;
    * for 3+ labels, returns the last two labels — or the last three when the
      trailing two are a known multi-part public suffix (e.g. ``co.uk``).
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    # Strip an IPv4/host :port (leave bracketed IPv6 and bare IPv6 alone).
    if host.count(":") == 1 and not host.startswith("["):
        host = host.split(":", 1)[0]
    # IP literals and localhost-style single labels: nothing to reduce.
    if _looks_like_ip(host) or "." not in host:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    # A wildcard-host service hands any subdomain to anyone, so reducing further
    # would group unrelated applications under one domain. Keep the whole host.
    for _i in range(2, len(labels)):
        if ".".join(labels[-_i:]) in _WILDCARD_HOST_SUFFIXES:
            return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


def _looks_like_ip(host: str) -> bool:
    """True for an IPv4 dotted-quad or an IPv6 literal (bracketed or bare)."""
    if host.startswith("[") or host.count(":") >= 2:
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return True
    return False


def same_registrable_domain(host_a: str, host_b: str) -> bool:
    """True when both hosts reduce to the SAME non-empty registrable domain.

    Fail-closed: an empty result on either side (unparseable host) is never a
    match. Callers use this to compute the ``is_login_domain`` flag passed to
    :func:`classify_request` for the AUTH phase.
    """
    reg_a = registrable_domain(host_a)
    reg_b = registrable_domain(host_b)
    return bool(reg_a) and reg_a == reg_b


# ─── Request-field extraction ──────────────────────────────────────────────


def _request_fields(url: str, button_name: str) -> dict[str, str]:
    """Build the ``{button_name, url_path, url_query}`` haystack for rule
    matching. An unparseable url degrades to empty path/query (never raises)."""
    try:
        parts = urlsplit(url or "")
        url_path = parts.path or ""
        url_query = parts.query or ""
    except (ValueError, TypeError):
        url_path = ""
        url_query = ""
    return {
        "button_name": (button_name or "").strip(),
        "url_path": url_path,
        "url_query": url_query,
    }


def _first_match(rules, fields: Mapping[str, str]):
    """Return the first rule whose regex matches ``fields``, else ``None``."""
    for rule in rules:
        if rule.matches(fields):
            return rule
    return None


# ─── Classification: is a control an irreversible never-click leaf? ─────────


def classify_action_verb(
    button_name: str, url: str, refuse_pack: "RefusePack | None",
) -> VerbClassification:
    """Classify a candidate control by its accessible name (+ its url) against
    the refuse pack's irreversible verbs.

    Used by the inventory to mark danger controls as NEVER-CLICK leaves. An
    ``allow_override`` match wins over an irreversible match (the reviewed,
    versioned escape hatch). Fail-closed: with no pack, nothing can be proven
    safe, so the control is treated as irreversible.
    """
    if not isinstance(refuse_pack, RefusePack):
        return VerbClassification(
            irreversible=True, rule_id=GuardRule.NO_REFUSE_PACK, severity="critical",
            reason="no refuse pack loaded — cannot prove the control is safe",
        )

    fields = _request_fields(url, button_name)

    override = _first_match(refuse_pack.allow_overrides, fields)
    if override is not None:
        return VerbClassification(
            irreversible=False, rule_id=override.id, severity=override.severity,
            reason=f"allow-override {override.id!r}: {override.description}",
        )

    verb = _first_match(refuse_pack.irreversible_verbs, fields)
    if verb is not None:
        return VerbClassification(
            irreversible=True, rule_id=verb.id, severity=verb.severity,
            reason=f"irreversible verb {verb.id!r}: {verb.description}",
        )

    return VerbClassification(irreversible=False)


def _mutation_signal_get(
    url: str, refuse_pack: "RefusePack", button_name: str,
) -> "RefuseRule | None":
    """The mutation-signal-GET rule that fires for this url, or ``None``.

    URL-precise by design (query/path shaped): ordinary read navigation is
    never starved — a GET is a mutation signal only when its URL itself
    declares a mutation (logout, action=delete, method-override, destructive
    REST path)."""
    fields = _request_fields(url, button_name)
    return _first_match(refuse_pack.mutation_signal_get_rules, fields)


# ─── The decision the route handler consults for EVERY request ─────────────


def classify_request(
    method: str,
    url: str,
    phase: "Phase | str",
    refuse_pack: "RefusePack | None",
    is_login_domain: bool,
    action_button_name: str = "",
    *,
    attestation: "Attestation | None" = None,
    submit_flow_approved: bool = False,
    now_ms: int | None = None,
    walk_attested: bool = False,
) -> GuardDecision:
    """Decide whether one network request may proceed. PURE + fail-closed.

    The 6 positional parameters are the pinned shared-conventions signature
    (``method, url, phase, refuse_pack, is_login_domain, action_button_name``).
    The keyword-only params gate the SUBMIT and WALK phases; they default to the
    safe (refusing) values so a caller that supplies only the positional args
    can NEVER accidentally authorise a mutating submit or a walk write.

    Rules:
      * unknown method / phase / missing pack        → ABORT (fail-closed);
      * EXPLORE  : allow GET/HEAD/OPTIONS (unless a mutation-signal GET);
                   ABORT every mutating method;
      * AUTH     : allow reads (unless mutation-signal GET); allow the login
                   POST ONLY on the same registrable domain and not carrying an
                   irreversible verb; ABORT every other mutation. The ≤10-req /
                   ≤30-s window is caller-enforced (design §3.2);
      * SUBMIT   : allow reads (unless mutation-signal GET); allow a mutating
                   request ONLY when a valid attestation is present AND the flow
                   is approved AND it does not match an irreversible verb;
      * WALK     : allow reads (unless mutation-signal GET); allow a mutating
                   request ONLY when ``walk_attested`` — which the caller sets
                   from a VERIFIED platform provisioning proof, never from a
                   dispatch field — and it does not match an irreversible verb.
                   The per-step mutation budget and the actuation window are
                   caller-enforced (:meth:`app.guard_context.GuardContext.decide`),
                   exactly as the AUTH and SUBMIT windows already are.

    Every returned :class:`GuardDecision` carries a stable ``rule_id`` +
    ``reason``; a block also carries the ``guard_event`` ``event_kind``.
    """
    # ── Fail-closed preconditions ─────────────────────────────────────────
    if not isinstance(refuse_pack, RefusePack):
        return _block(GuardRule.NO_REFUSE_PACK, EVENT_BLOCKED_METHOD, "critical",
                      "no refuse pack loaded — every request is refused")

    normalized_method = (method or "").strip().upper()
    if normalized_method not in KNOWN_METHODS:
        return _block(GuardRule.UNKNOWN_METHOD, EVENT_BLOCKED_METHOD, "critical",
                      f"unknown/unsupported HTTP method {method!r} — refused")

    resolved_phase = _coerce_phase(phase)
    if resolved_phase is None:
        return _block(GuardRule.UNKNOWN_PHASE, EVENT_BLOCKED_METHOD, "critical",
                      f"unknown crawl phase {phase!r} — refused")

    is_read = normalized_method in READ_METHODS

    # ── Reads: identical mutation-signal check in EVERY phase ─────────────
    # A logout / action=delete / method-override GET must abort whether we are
    # exploring, authenticating, or submitting.
    if is_read:
        signal = _mutation_signal_get(url, refuse_pack, action_button_name)
        if signal is not None:
            return _block(signal.id, EVENT_REFUSED_VERB_GET, signal.severity,
                          f"mutation-signal GET refused ({signal.id}): "
                          f"{signal.description}")

    # ── Phase EXPLORE ─────────────────────────────────────────────────────
    if resolved_phase is Phase.EXPLORE:
        if is_read:
            return _allow(GuardRule.EXPLORE_READ_OK,
                          f"{normalized_method} allowed in EXPLORE (read-only)")
        return _block(GuardRule.EXPLORE_MUTATION_BLOCKED, EVENT_BLOCKED_METHOD,
                      "critical",
                      f"{normalized_method} blocked in EXPLORE — no mutation "
                      f"is permitted outside AUTH/SUBMIT")

    # ── Phase WALK (M1.3 controlled persistence) ──────────────────────────
    # A NARROWER grant than SUBMIT, not a wider EXPLORE. Reaching the allow at
    # the bottom requires a cryptographically verified platform provisioning
    # proof; the per-step budget and the actuation window are enforced by the
    # caller BEFORE this function is reached, so a mutation that gets here has
    # already been counted against a bounded, audited allowance.
    if resolved_phase is Phase.WALK:
        if is_read:
            return _allow(GuardRule.WALK_READ_OK,
                          f"{normalized_method} allowed in WALK (read-only)")
        if not walk_attested:
            return _block(GuardRule.WALK_NO_ATTESTATION, EVENT_BLOCKED_METHOD,
                          "critical",
                          f"{normalized_method} refused in WALK — no verified "
                          f"platform disposable-environment attestation")
        # IRREVERSIBLE VERBS NEVER CROSS HERE.
        #
        # SUBMIT deliberately lets one through on a disposable env, because a
        # human named that flow and the product exists to prove checkouts and
        # cancellations actually work. WALK has no human in the loop: it fires
        # on every wizard step the crawler chooses for itself. "Save Draft"
        # earning the same rights as "Delete Account" is the escalation this
        # feature would otherwise ship, so the danger gate stays absolute here
        # even though the environment is attested throwaway.
        verb = classify_action_verb(action_button_name, url, refuse_pack)
        if verb.irreversible:
            return _block(GuardRule.WALK_IRREVERSIBLE_BLOCKED,
                          EVENT_REFUSED_VERB_GET, verb.severity,
                          f"{normalized_method} refused in WALK — carries an "
                          f"irreversible verb: {verb.reason}")
        return _allow(GuardRule.WALK_MUTATION_OK,
                      f"{normalized_method} allowed in WALK (attested disposable "
                      f"environment, non-irreversible; budget + window are "
                      f"caller-enforced and audited)")

    # ── Phase AUTH ────────────────────────────────────────────────────────
    if resolved_phase is Phase.AUTH:
        if is_read:
            return _allow(GuardRule.AUTH_READ_OK,
                          f"{normalized_method} allowed in AUTH (read-only)")
        if normalized_method != "POST":
            return _block(GuardRule.AUTH_NONLOGIN_MUTATION_BLOCKED,
                          EVENT_BLOCKED_METHOD, "critical",
                          f"{normalized_method} blocked in AUTH — only the "
                          f"login POST is permitted")
        if not is_login_domain:
            return _block(GuardRule.AUTH_OFFDOMAIN_POST_BLOCKED,
                          EVENT_BLOCKED_METHOD, "critical",
                          "login POST blocked in AUTH — not on the login's "
                          "registrable domain")
        verb = classify_action_verb(action_button_name, url, refuse_pack)
        if verb.irreversible:
            return _block(verb.rule_id, EVENT_REFUSED_VERB_GET, verb.severity,
                          f"login POST refused in AUTH — carries an "
                          f"irreversible verb: {verb.reason}")
        return _allow(GuardRule.AUTH_LOGIN_POST_OK,
                      "login POST allowed in AUTH (same registrable domain, "
                      "no irreversible verb; window is caller-enforced)")

    # ── Phase SUBMIT ──────────────────────────────────────────────────────
    if resolved_phase is Phase.SUBMIT:
        if is_read:
            return _allow(GuardRule.SUBMIT_READ_OK,
                          f"{normalized_method} allowed in SUBMIT (read-only)")
        # Mutating request — three independent gates, each a distinct refusal.
        # NOT ``now_ms``: that is the crawl's MONOTONIC clock (ms since start),
        # used correctly above for the auth/submit WINDOWS. Attestation
        # freshness is a wall-clock question and is read from the wall clock
        # inside is_submit_capable (T-SEC-08).
        if attestation is None or not attestation.is_submit_capable():
            return _block(GuardRule.SUBMIT_NO_ATTESTATION, EVENT_BLOCKED_METHOD,
                          "critical",
                          f"{normalized_method} refused in SUBMIT — no valid "
                          f"disposable-env attestation present")
        if not submit_flow_approved:
            return _block(GuardRule.SUBMIT_NOT_APPROVED, EVENT_BLOCKED_METHOD,
                          "critical",
                          f"{normalized_method} refused in SUBMIT — the flow is "
                          f"not human-approved")
        # An IRREVERSIBLE verb is allowed here, and recorded.
        #
        # Reaching this line already required a valid, unexpired, attributed
        # DISPOSABLE-env attestation — the operator's signed statement that this
        # target is throwaway. On a throwaway environment there is nothing for a
        # delete or a purchase to ruin, and refusing them meant the product could
        # never prove the flows a business most needs proven: the checkout, the
        # cancellation, the submitted application.
        #
        # It is still WRITTEN DOWN. The verdict carries the verb, so evidence shows
        # exactly which irreversible action was taken and under whose attestation —
        # removing the block must not also remove the record.
        verb = classify_action_verb(action_button_name, url, refuse_pack)
        if verb.irreversible:
            return _allow(GuardRule.SUBMIT_MUTATION_OK,
                          f"{normalized_method} allowed in SUBMIT on a DISPOSABLE "
                          f"env under attestation+approval — irreversible verb "
                          f"CROSSED and recorded: {verb.reason}")
        return _allow(GuardRule.SUBMIT_MUTATION_OK,
                      f"{normalized_method} allowed in SUBMIT (attested, "
                      f"approved, not an irreversible verb)")

    # ── Unreachable: every Phase member handled above; fail closed ────────
    return _block(GuardRule.MALFORMED, EVENT_BLOCKED_METHOD, "critical",
                  "unhandled classification path — refused")


def _allow(rule_id: str, reason: str) -> GuardDecision:
    return GuardDecision(allow=True, reason=reason, rule_id=rule_id,
                         event_kind=EVENT_ALLOW, severity="")


def _block(rule_id: str, event_kind: str, severity: str, reason: str) -> GuardDecision:
    return GuardDecision(allow=False, reason=reason, rule_id=rule_id,
                         event_kind=event_kind, severity=severity)

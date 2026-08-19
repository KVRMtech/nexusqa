"""DISCOVERED BUSINESS RULES — knowledge that outlives the crawl that found it
(M1.7 / T-GW-04).

WHAT THE ENGINE ALREADY DOES, AND WHY IT WAS NOT ENOUGH.
``walker._answer_to_unblock`` runs a genuine experiment.  The app has disabled
its own forward control; the walk answers ONE declined question, re-reads the
page, and lets the application render its own verdict.  When the control
enables, the crawl has PROVED a rule that exists nowhere in the markup — a
``.min(1)`` in a zod schema, a hand-written ``canAdvance()`` — and it writes the
sentence::

    "Continue requires an answer to 'Health Conditions' before it is enabled
     (proven: the app enabled it when the agent answered)"

into ``self._advance_blocked[i]["business_rule"]``.  A LIST ON THE CRAWLER
OBJECT.  It rides out in ``coverage.advance_blocked``, qe-central counts how
many there were in ``fleet_funnel``, and there the knowledge stops: nothing
persists it as a rule, nothing indexes it, and no dispatch has ever handed one
back to a crawl.  So every crawl of the same application re-runs the same
experiment against the same checkbox to re-derive the same sentence — paying the
set/re-observe/verify round trip, and risking the revert path, to learn
something the tenant already knew.  Learning happened and never accumulated.

WHAT THIS MODULE ADDS.  A stable IDENTITY for a rule, a versioned record shape,
and the lookup a crawl uses to consume what earlier crawls proved.

  * :func:`rule_key` — the identity.  Derived from the URL TEMPLATE (so an id in
    the path does not mint a new rule per record), the blocked control's
    normalised label, and the field that unblocked it.  Value-free: labels are
    product UI text, never anything a user typed.
  * :class:`DiscoveredRule` — the persisted shape, carrying its own
    ``schema_version`` so a later reader can migrate rather than guess.
  * :class:`KnownRules` — the read side.  ``lookup`` answers "has anyone proved
    what unblocks THIS control on THIS page before?"

WHAT A REUSED RULE DOES AND DOES NOT SKIP.  It skips the EXPERIMENT — the
candidate search, the re-observation, the enablement check, the revert-on-failure
— not the ACTION.  The walk still sets the control, because a rule is knowledge
about the application, never a substitute for having done the thing.  A crawl
that "reused" a rule and did not act on it would be exactly the green-wash this
milestone is closing, in a new place.

TENANCY IS NOT THIS MODULE'S JOB.  Rules arrive already scoped to one tenant and
one app by qe-central (which owns the RLS-forced table); this side never sees
another tenant's rules and has no way to ask for them.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .crawl_constants import url_template

#: Bumped when the persisted shape changes meaning.  A consumer that reads a
#: version it does not know MUST ignore the rule rather than guess at it — an
#: unread rule costs one repeated experiment; a misread one drives the crawl.
RULE_SCHEMA_VERSION = 1

#: The only rule kind this milestone proves.  Named rather than implied so the
#: table can hold the next kind (a cross-field dependency, a value range the app
#: rejected) without a migration.
KIND_ADVANCE_GATE = "advance_gate"

#: Provenance marker written on a field answered from a REUSED rule, distinct
#: from ``PROV_UNBLOCK`` (which means "this crawl proved it just now").  Keeping
#: them apart is what lets a report say whether a claim rests on evidence from
#: THIS run or on inherited evidence — conflating them would make an inherited
#: rule indistinguishable from a fresh proof.
PROV_KNOWN_RULE = "known_rule"

_WS_RX = re.compile(r"\s+")


def norm_label(text: Any) -> str:
    """A control label reduced to its comparable form (lowercased, collapsed).

    The same normalisation the walker uses on accessible names, restated here so
    this module stays importable without the walker (which pulls in the browser
    stack).  A rule key computed two different ways is a rule that never matches.
    """
    return _WS_RX.sub(" ", str(text or "").strip()).lower()


def rule_key(*, url: str, blocked_label: str, field_label: str,
             kind: str = KIND_ADVANCE_GATE) -> str:
    """The stable identity of a rule.  PURE and deterministic.

    Keyed on the URL TEMPLATE, not the URL: ``/application/8814/health`` and
    ``/application/9137/health`` are the same page of the same wizard, and a rule
    keyed on the raw URL would be re-proved once per applicant forever.
    """
    basis = "|".join((
        kind,
        url_template(url or ""),
        norm_label(blocked_label),
        norm_label(field_label),
    ))
    return "rule:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class DiscoveredRule:
    """One rule an experiment PROVED, in the shape that persists.

    ``proof`` is the sentence the application itself justified — it is kept
    verbatim rather than regenerated on read, because the wording is the record
    of what was observed, and a rule whose proof is rewritten later by different
    code is no longer evidence of anything.
    """

    key: str
    kind: str
    url_template: str
    blocked_label: str
    field_label: str
    proof: str = ""
    schema_version: int = RULE_SCHEMA_VERSION

    def as_dict(self) -> dict:
        return {
            "key": self.key, "kind": self.kind, "url_template": self.url_template,
            "blocked_label": self.blocked_label, "field_label": self.field_label,
            "proof": self.proof, "schema_version": self.schema_version,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Optional["DiscoveredRule"]:
        """Parse a persisted rule; ``None`` when it cannot be trusted.

        Fail-closed on the schema version: a rule written by a FUTURE explorer may
        mean something this one would misapply, and the cost of ignoring it (one
        repeated experiment) is far below the cost of acting on a misreading.
        """
        if not isinstance(raw, Mapping):
            return None
        try:
            version = int(raw.get("schema_version") or RULE_SCHEMA_VERSION)
        except (TypeError, ValueError):
            return None
        if version > RULE_SCHEMA_VERSION:
            return None
        key = str(raw.get("key") or "").strip()
        blocked = str(raw.get("blocked_label") or "").strip()
        field_label = str(raw.get("field_label") or "").strip()
        if not (key and blocked and field_label):
            return None
        return cls(
            key=key[:64],
            kind=str(raw.get("kind") or KIND_ADVANCE_GATE)[:32],
            url_template=str(raw.get("url_template") or "")[:500],
            blocked_label=blocked[:120],
            field_label=field_label[:120],
            proof=str(raw.get("proof") or "")[:500],
            schema_version=version,
        )


def discover(*, url: str, blocked_label: str, field_label: str,
             proof: str = "") -> DiscoveredRule:
    """Mint the rule an experiment just proved."""
    return DiscoveredRule(
        key=rule_key(url=url, blocked_label=blocked_label, field_label=field_label),
        kind=KIND_ADVANCE_GATE,
        url_template=url_template(url or "")[:500],
        blocked_label=str(blocked_label or "")[:120],
        field_label=str(field_label or "")[:120],
        proof=str(proof or "")[:500],
    )


class KnownRules:
    """The rules earlier crawls of THIS app proved, indexed for lookup.

    Constructed from whatever qe-central sent on the dispatch.  An empty instance
    is the pre-M1.7 behaviour exactly: every lookup misses, every block runs the
    full experiment, and nothing about the crawl changes.  That is the intended
    degradation whenever the store is empty, unreachable, or disabled.
    """

    def __init__(self, rules: Iterable[Mapping[str, Any]] = ()) -> None:
        self._by_key: dict[str, DiscoveredRule] = {}
        #: (url_template, normalised blocked label) -> rule.  The lookup the walk
        #: actually performs: it knows what is blocked and where, and is asking
        #: WHICH FIELD unblocks it.
        self._by_site: dict[tuple, DiscoveredRule] = {}
        self._hits = 0
        self._misses = 0
        for raw in rules or ():
            rule = DiscoveredRule.from_mapping(raw)
            if rule is None:
                continue
            self._by_key[rule.key] = rule
            self._by_site.setdefault(
                (rule.url_template, norm_label(rule.blocked_label)), rule)

    def __len__(self) -> int:
        return len(self._by_key)

    def __bool__(self) -> bool:
        return bool(self._by_key)

    def lookup(self, *, url: str, blocked_label: str) -> Optional[DiscoveredRule]:
        """The rule proved for this blocked control on this page, if any.

        Counts hits and misses as a side effect — the reuse RATE is one of the
        milestone's quality metrics, and deriving it after the fact from logs is
        how a metric ends up unmeasurable.
        """
        site = (url_template(url or ""), norm_label(blocked_label))
        rule = self._by_site.get(site)
        if rule is None:
            self._misses += 1
        else:
            self._hits += 1
        return rule

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def stats(self) -> dict:
        looked_up = self._hits + self._misses
        return {
            "known": len(self._by_key),
            "lookups": looked_up,
            "hits": self._hits,
            "misses": self._misses,
            #: The headline metric: of the blocked advances this crawl met, how
            #: many were answered from inherited knowledge instead of a fresh
            #: experiment.  0.0 with no lookups — never a division by zero, and
            #: never a flattering 1.0 for a crawl that met no blocks at all.
            "reuse_rate": round(self._hits / looked_up, 4) if looked_up else 0.0,
        }


class RuleLedger:
    """Rules this crawl PROVED, deduped by key, in discovery order.

    Deduped because a wizard revisits the same blocked step across branches and a
    ledger that recorded one rule per encounter would report the same discovery
    as several, inflating exactly the number the milestone asks us to measure.
    """

    def __init__(self, max_rules: int = 200) -> None:
        self._rules: dict[str, DiscoveredRule] = {}
        self._max = int(max_rules)

    def add(self, rule: DiscoveredRule) -> bool:
        """Record ``rule``; False when it was already known to this crawl."""
        if rule.key in self._rules or len(self._rules) >= self._max:
            return False
        self._rules[rule.key] = rule
        return True

    def __len__(self) -> int:
        return len(self._rules)

    def as_list(self) -> list:
        return [rule.as_dict() for rule in self._rules.values()]


__all__ = [
    "RULE_SCHEMA_VERSION", "KIND_ADVANCE_GATE", "PROV_KNOWN_RULE",
    "DiscoveredRule", "KnownRules", "RuleLedger",
    "discover", "norm_label", "rule_key",
]

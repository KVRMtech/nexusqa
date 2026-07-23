"""R6 — promote a recurring data-plane HEAL into a candidate permanent strategy.

The proven-control ledger records every oracle-verified heal (reanchor /
interaction / control_kind / nav / …). When the SAME kind of heal keeps proving
green across DISTINCT apps, that is the signal a one-off repair should graduate
into a permanent, code-level capability that benefits every future client — the
'continuously improving through real-world execution' loop the requirement
describes.

This miner reads ledger entries and emits PROMOTION CANDIDATES: a heal pattern
seen on >= ``min_apps`` distinct apps, ranked by breadth. It is PURE + read-only:
it neither writes code nor changes the ledger. Each candidate is a human-gated
proposal (same doctrine as the Recovery Agent) — a maintainer reviews it and, if
apt, lands a permanent UACR recipe + its regression test. The agent never
self-modifies the product.

Grounded + honest: a candidate is emitted ONLY from heals that ALREADY proved
green (confirmed_count >= 1, not quarantined); breadth is counted by DISTINCT
app scope, so ten scenarios in one app never masquerade as a cross-client
pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Fix kinds worth promoting into a permanent interaction STRATEGY. Pure
# navigation/entry-URL corrections (nav/nav_recover) are per-app data, not a
# reusable interaction primitive, so they are excluded from promotion.
_PROMOTABLE = frozenset({"control_kind", "interaction", "reanchor", "wait"})

#: A heal must recur across at least this many DISTINCT apps to be a candidate.
DEFAULT_MIN_APPS = 3


@dataclass
class PromotionCandidate:
    fix_kind: str
    strategy_key: str            # the specific pattern (payload recipe / control kind)
    app_count: int               # distinct apps that needed it (the breadth signal)
    total_confirmations: int     # sum of oracle-proven confirmations across apps
    example_labels: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)

    def as_proposal(self) -> dict:
        return {
            "kind": "capability_promotion_proposal",
            "status": "proposed",
            "apply_requires": ("maintainer approval — land a permanent UACR recipe "
                               "+ its regression test; the agent never self-modifies"),
            "fix_kind": self.fix_kind,
            "strategy_key": self.strategy_key,
            "app_count": self.app_count,
            "total_confirmations": self.total_confirmations,
            "evidence": {
                "distinct_apps": self.apps[:20],
                "example_controls": self.example_labels[:10],
            },
            "rationale": (
                f"The '{self.fix_kind}' heal '{self.strategy_key}' proved green on "
                f"{self.app_count} distinct apps ({self.total_confirmations} confirmed "
                f"repairs). A recurring cross-app heal should graduate into a permanent "
                f"capability so it is applied on the FIRST pass, not re-healed per app."),
        }


def _strategy_key(entry: dict) -> str:
    """The pattern a heal represents, from its payload — the thing that would
    become a permanent recipe. Falls back to the fix_kind."""
    payload = entry.get("payload") or {}
    for k in ("recipe", "control_kind", "kind", "strategy", "wait_kind"):
        v = str(payload.get(k) or "").strip()
        if v:
            return v
    return str(entry.get("fix_kind") or "")


def mine_promotions(
    entries: list[dict], *, min_apps: int = DEFAULT_MIN_APPS,
) -> list[PromotionCandidate]:
    """Group PROVEN, non-quarantined heals by (fix_kind, strategy_key); emit a
    candidate for each pattern seen on >= ``min_apps`` distinct apps, ranked by
    breadth then confirmations."""
    groups: dict[tuple, dict] = {}
    for e in entries or []:
        fk = str(e.get("fix_kind") or "").strip()
        if fk not in _PROMOTABLE:
            continue
        if e.get("invalidated_at"):            # quarantined — not a durable signal
            continue
        if int(e.get("confirmed_count") or 0) < 1:
            continue
        app = str(e.get("app_key") or e.get("app_fingerprint") or "").strip()
        if not app:
            continue
        key = (fk, _strategy_key(e))
        g = groups.setdefault(key, {"apps": set(), "confirms": 0, "labels": []})
        g["apps"].add(app)
        g["confirms"] += int(e.get("confirmed_count") or 0)
        lbl = str(e.get("label") or "").strip()
        if lbl and lbl not in g["labels"]:
            g["labels"].append(lbl)

    out: list[PromotionCandidate] = []
    for (fk, skey), g in groups.items():
        if len(g["apps"]) >= min_apps:
            out.append(PromotionCandidate(
                fix_kind=fk, strategy_key=skey, app_count=len(g["apps"]),
                total_confirmations=g["confirms"],
                example_labels=g["labels"], apps=sorted(g["apps"])))
    out.sort(key=lambda c: (c.app_count, c.total_confirmations), reverse=True)
    return out


def mine_to_dicts(entries: list[dict], *, min_apps: int = DEFAULT_MIN_APPS) -> list[dict]:
    return [c.as_proposal() for c in mine_promotions(entries, min_apps=min_apps)]

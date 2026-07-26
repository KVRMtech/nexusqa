"""Persona scale (P5) — trait-based selection, credential staleness, and the
provisioning manifest. Pure functions, ZERO LLM, ZERO I/O.

At 100+ clients you do not pin every run to a persona_id by hand. You say "run
as any senior-large-family member" and the matrix resolves a concrete persona;
you ask "which member cards went stale when UAT's data rolled to epoch N" and get
an honest due-list; you provision a whole app's members from one import. This
module is the deterministic core behind all three — every answer carries the
reason that produced it, and an unknown is UNKNOWN, never a fabricated match.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ── Trait-based persona selection ────────────────────────────────────────────

def select_persona(personas: list[dict], *, traits: list | None = None,
                   behavior_class: str = "") -> dict:
    """Resolve a concrete persona from a trait/behavior-class request.

    A persona is a CANDIDATE when it carries every requested trait and (if a
    class is asked) sits in that behavior class. Among candidates the most
    SPECIFIC wins — fewest surplus traits, then name — so "senior-large-family"
    resolves the same persona every time (deterministic, never random). Returns
    {persona, candidates, ambiguous, reason}; persona is None when nothing
    matches (an honest miss, never a wrong member).
    """
    want = {str(t).strip().lower() for t in (traits or []) if str(t).strip()}
    wc = (behavior_class or "").strip().lower()
    cands = []
    for p in (personas or []):
        if (p.get("status") or "active") != "active":
            continue
        pc = (p.get("behavior_class") or "").strip().lower()
        pt = {str(t).strip().lower() for t in (p.get("traits") or [])}
        if wc and pc != wc:
            continue
        if not want.issubset(pt):
            continue
        cands.append(p)
    if not cands:
        return {"persona": None, "candidates": [], "ambiguous": False,
                "reason": (f"no active persona carries {sorted(want) or 'the request'}"
                           + (f" in behavior class '{wc}'" if wc else "")
                           + " — nothing matched, so nothing is chosen. Define a "
                             "persona or relax the request (no member is guessed).")}
    # most specific first: fewest surplus traits, then name
    cands.sort(key=lambda p: (len({str(t).lower() for t in (p.get("traits") or [])}) - len(want),
                              (p.get("name") or "")))
    chosen = cands[0]
    return {"persona": chosen, "candidates": [c.get("persona_id") for c in cands],
            "ambiguous": len(cands) > 1,
            "reason": (f"selected '{chosen.get('name')}'"
                       + (f" (class '{wc}')" if wc else "")
                       + (f"; {len(cands)} personas matched, chose the most specific."
                          if len(cands) > 1 else "; unique match."))}


# ── Credential staleness (keyed to the environment data epoch) ───────────────

STALE_NEVER = "never_verified"
STALE_EPOCH = "stale_epoch"
STALE_TTL = "stale_ttl"
FRESH = "fresh"


def _parse_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def card_staleness(card: dict | None, env: dict | None, *, now: datetime,
                   ttl_days: int = 30) -> dict:
    """Is a persona's credential card still trustworthy for an environment?

    A card is STALE when the environment's data epoch has rolled past the epoch
    the card was last verified against (the member's data may have changed), or
    when the last verification is older than the TTL, or it was never verified.
    A stale card is a re-verify SIGNAL, not a failure — we never run a member on
    a card we can no longer vouch for and call the result green.
    """
    card = card or {}
    env = env or {}
    if not card.get("present") and "verify_status" not in card:
        return {"state": STALE_NEVER, "reason": "no credential card on file for this environment."}
    if (card.get("verify_status") or "unverified") != "verified" or not card.get("last_verified_at"):
        return {"state": STALE_NEVER,
                "reason": "the card has never been verified against this environment."}
    verified_epoch = str(card.get("verified_epoch") or "")
    env_epoch = str(env.get("data_epoch") or "")
    if env_epoch and verified_epoch and env_epoch != verified_epoch:
        return {"state": STALE_EPOCH,
                "reason": (f"the environment data epoch rolled to '{env_epoch}' since this "
                           f"card was verified against '{verified_epoch}' — the member's "
                           "data may have changed; re-verify before trusting it.")}
    last = _parse_dt(card.get("last_verified_at"))
    if last is not None and (now - last) > timedelta(days=max(1, int(ttl_days))):
        age = (now - last).days
        return {"state": STALE_TTL,
                "reason": f"last verified {age} days ago (TTL {ttl_days}d) — re-verify."}
    return {"state": FRESH, "reason": "verified against the current epoch and within TTL."}


def staleness_report(cards_by_persona: dict[str, dict], env: dict | None, *,
                     now: datetime, ttl_days: int = 30) -> dict:
    """Per-persona staleness for one environment, with the due-list summarized.

    ``cards_by_persona`` maps persona_id → card status dict. Returns the full
    grid plus ``due`` (persona_ids needing re-verification) — the honest answer
    to "which members must I re-verify before the next regression run?".
    """
    per: dict[str, dict] = {}
    due: list[str] = []
    counts = {FRESH: 0, STALE_EPOCH: 0, STALE_TTL: 0, STALE_NEVER: 0}
    for pid, card in (cards_by_persona or {}).items():
        s = card_staleness(card, env, now=now, ttl_days=ttl_days)
        per[pid] = s
        counts[s["state"]] = counts.get(s["state"], 0) + 1
        if s["state"] != FRESH:
            due.append(pid)
    return {"per_persona": per, "due": due, "due_count": len(due),
            "counts": counts, "ttl_days": ttl_days,
            "environment_epoch": str((env or {}).get("data_epoch") or "")}


# ── Impersonation recipe scaffold (env-dependent switch-user) ─────────────────

def build_impersonation_recipe(*, admin_login_steps: list, impersonate_path: str,
                               member_slot: str = "member_number",
                               submit_selector: str = "") -> dict:
    """Scaffold a recipe for an env where a run reaches a member by ADMIN
    impersonation (a switch-user endpoint) instead of a member login. It is an
    ordinary recipe — admin login steps, then goto the impersonation path, fill
    the member slot, submit — so it verifies and runs through the SAME machinery.
    Secrets never appear here; the member number rides its slot, the admin
    credentials ride theirs. Returns {steps, slots} ready to POST to /recipes.
    """
    steps = list(admin_login_steps or [])
    steps.append({"action": "goto", "path": impersonate_path or "/"})
    steps.append({"action": "fill", "slot": member_slot,
                  "selector": f"[name='{member_slot}'], #{member_slot}"})
    steps.append({"action": "click", "selector": submit_selector or "button[type='submit']"})
    # union of admin-login slots + the member slot (dedup by name)
    slots = []
    seen = set()
    for st in admin_login_steps or []:
        sl = st.get("slot")
        if sl and sl not in seen:
            slots.append({"name": sl, "type": "secret"}); seen.add(sl)
    if member_slot not in seen:
        slots.append({"name": member_slot, "type": "plain"})
    return {"steps": steps, "slots": slots,
            "note": ("An impersonation recipe reaches a member via an admin "
                     "switch-user endpoint — env-dependent, but it verifies and "
                     "runs exactly like a login recipe. No secret is embedded.")}


__all__ = [
    "select_persona",
    "STALE_NEVER", "STALE_EPOCH", "STALE_TTL", "FRESH",
    "card_staleness", "staleness_report",
    "build_impersonation_recipe",
]

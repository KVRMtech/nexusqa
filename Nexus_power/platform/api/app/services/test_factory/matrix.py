"""Which member × environment combinations can actually run, at a glance.

Everything the operator needed to know was already computable and nowhere visible:
you discovered that a card was missing, orphaned or unproven by dispatching a run
and reading the refusal. That is a slow way to learn a fact the server knows before
the run starts, and it teaches people to treat a BLOCKED run as noise.

So: members down, environments across, one verdict per cell, from the SAME
derivation the dispatch gate applies (``card_state`` for the credential, and the
environment's own posture for the target). If a cell says ready, a run from it is
not refused for either reason; if it says blocked, the run would have been.

Two independent axes, and they are kept apart on purpose:

  * the CREDENTIAL axis — can this member perform the recorded login here?
  * the TARGET axis — does this environment permit this kind of run at all?

A production environment that is default-deny blocks every member equally, and
saying "no card" there would send the operator to provision credentials that were
never the problem.

Pure — no DB, no I/O. The caller loads the rows and renders the result.
"""
from __future__ import annotations

from . import card_state
from . import persona_governance

__all__ = ["build", "CELL_ORDER"]

#: Worst-first. A screen that leads with what is broken is read differently from one
#: that leads with what is fine.
CELL_ORDER = ["stale_slots", "no_card", "blocked_posture", "no_recipe",
              "unproven", "ready"]

_BLOCKED_POSTURE = "blocked_posture"


def build(*, personas: list, environments: list, cards: list,
          recipe: dict | None, mutating: bool = True,
          legacy_persona_ids: set | None = None) -> dict:
    """Assemble the matrix.

    ``personas``      list_personas output (active members)
    ``environments``  list_environments output
    ``cards``         all_credential_status output (slot NAMES only — no secrets)
    ``recipe``        the artifact's ACTIVE login recipe
    ``mutating``      judge the posture axis for a mutating run (the default a run
                      takes). A read-only run is allowed on a locked environment, so
                      the matrix says which question it answered.

    Returns ``{members, environments, cells, summary, mutating}`` where ``cells`` is
    keyed ``"<persona_id>::<environment_id>"``.
    """
    by_key = {f"{c.get('persona_id')}::{c.get('environment_id')}": c
              for c in (cards or [])}
    legacy = set(legacy_persona_ids or ())

    # The posture verdict is per ENVIRONMENT, so compute it once per column rather
    # than once per cell — and so the UI can label the column, not just the cells.
    posture: dict = {}
    for env in (environments or []):
        gate = persona_governance.gate_dispatch(env, mutating_requested=bool(mutating))
        posture[env.get("environment_id")] = {
            "allowed": gate["allowed"], "posture": gate["posture"],
            "reason": " ".join(gate["reasons"]) if not gate["allowed"] else "",
        }

    cells: dict = {}
    counts: dict = {k: 0 for k in CELL_ORDER}
    for p in (personas or []):
        pid = p.get("persona_id")
        for env in (environments or []):
            eid = env.get("environment_id")
            st = card_state.evaluate(
                recipe=recipe, card=by_key.get(f"{pid}::{eid}"), environment=env,
                legacy_login_available=(pid in legacy))
            state, runnable = st["state"], st["runnable"]
            reason, note = st["reason"], st["detail"].get("note", "")

            # The TARGET axis is reported only when the CREDENTIAL axis is fine.
            # A cell that is both mis-provisioned and posture-locked has two
            # problems; naming the credential one first is right, because fixing
            # the posture would still leave the run refused.
            pv = posture.get(eid) or {}
            if runnable and not pv.get("allowed", True):
                state, runnable = _BLOCKED_POSTURE, False
                reason, note = "environment_policy", pv.get("reason", "")

            cells[f"{pid}::{eid}"] = {
                "persona_id": pid, "environment_id": eid,
                "state": state, "runnable": runnable, "reason": reason,
                "note": note,
                "proven": bool(st["proven"]) and state != _BLOCKED_POSTURE,
                "missing_slots": st["missing"], "unexpected_slots": st["unexpected"],
            }
            counts[state] = counts.get(state, 0) + 1

    return {
        "members": [{"persona_id": p.get("persona_id"), "name": p.get("name"),
                     "behavior_class": p.get("behavior_class", ""),
                     "traits": p.get("traits") or [],
                     "legacy": p.get("persona_id") in legacy}
                    for p in (personas or [])],
        "environments": [{"environment_id": e.get("environment_id"),
                          "label": e.get("label") or e.get("environment_id"),
                          "is_production": bool(e.get("is_production")),
                          "base_url": e.get("base_url", ""),
                          "data_epoch": e.get("data_epoch", ""),
                          "health_status": e.get("health_status", ""),
                          **(posture.get(e.get("environment_id")) or {})}
                         for e in (environments or [])],
        "cells": cells,
        "mutating": bool(mutating),
        "summary": {
            "total": len(cells),
            "runnable": sum(1 for c in cells.values() if c["runnable"]),
            "proven": sum(1 for c in cells.values() if c["proven"]),
            "counts": counts,
            "has_recipe": bool(recipe),
        },
    }

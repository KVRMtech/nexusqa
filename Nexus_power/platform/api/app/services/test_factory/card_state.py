"""Can this member log into this environment, and do we KNOW that or only hope it?

One derivation, three consumers:

  * **F4** — the dispatch gate. A card whose slots no longer match the active login
    is REFUSED, because the alternative is a skipped login: the suite executes
    unauthenticated and every failure is attributed to the application under test.
  * **F3** — the badge. ``verified`` must be falsifiable. A card proven against a
    superseded recipe, or against a data epoch that has since rolled, is not proven
    now, and saying otherwise is the same lie in a smaller font.
  * **F7** — the member × environment cell.

DERIVED, NOT STORED — deliberately. A stored flag is only as good as the last code
path that remembered to update it, and a card row can be written by a script, a
bulk import or a migration that never called the marker. Recomputing from the
recipe and the card on every read cannot be missed. The stored ``stale_slots``
marking (set at recipe-save time) exists to make the breakage VISIBLE immediately;
the gate never trusts it.

WHAT IS AND IS NOT A BLOCK
  * slots CHANGED           -> BLOCK. The card cannot fill the login. Not blocking
                               means running logged out and blaming the app.
  * no card at all          -> BLOCK, unless a legacy form-login backs this persona.
  * never proven            -> RUNNABLE. The first run IS the proof; blocking here
                               would make it impossible to ever prove a new card.
  * proof superseded/stale  -> RUNNABLE, badge withdrawn. The values may well still
                               be right; what expired is our evidence, not the card.
  * login_type_key differs  -> INFORMATIONAL ONLY. A card is per environment and
                               environments live on different hosts, so a different
                               login fingerprint is the NORMAL case across a matrix
                               row. Treating it as breakage would refuse every
                               multi-environment estate.

Pure — no DB, no I/O. The caller loads the recipe, the card status and the
environment row, and applies the verdict.
"""
from __future__ import annotations

from . import card_contract

__all__ = ["evaluate", "READY", "UNPROVEN", "STALE_SLOTS", "NO_CARD", "NO_RECIPE"]

READY = "ready"
UNPROVEN = "unproven"
STALE_SLOTS = "stale_slots"
NO_CARD = "no_card"
NO_RECIPE = "no_recipe"

#: States a run may proceed from. ``unproven`` is deliberately included — the first
#: authenticated run is what produces the proof, so refusing it would make
#: ``verified`` unreachable for every newly provisioned card.
_RUNNABLE = {READY, UNPROVEN}


def _slot_diff(required: list, held: list, allowed: list | None = None) -> dict:
    """``allowed`` are slots a card may legitimately carry without them being
    required — the ones filled only by OPTIONAL steps, for a screen some members
    never reach. Supplying one is preparedness, not a mismatch."""
    req, has = list(required or []), set(held or [])
    return {
        "required": req,
        "missing": [s for s in req if s not in has],
        "unexpected": sorted(has - set(req) - set(allowed or [])),
    }


def evaluate(*, recipe: dict | None, card: dict | None,
             environment: dict | None = None,
             legacy_login_available: bool = False,
             live_slot_values: dict | None = None) -> dict:
    """Judge one member × environment cell.

    ``recipe``      the artifact's ACTIVE login recipe (persona_store.get_recipe)
    ``card``        persona_store.credential_status output, or None
    ``environment`` the registered environment row, for its ``data_epoch``
    ``legacy_login_available``
                    True when a legacy form-login profile can stand in for a card
                    (the synthetic persona-0 path). Without this, every estate that
                    predates credential cards would be blocked on ``no_card`` — a
                    refusal for a configuration that works today.
    ``live_slot_values``
                    the DECRYPTED card, when the caller already holds it (the run
                    gate does). Preferred over the stored ``slot_names`` because it
                    is what will actually be handed to the login: a slot present but
                    EMPTY is not a value, and the stored name list cannot show that.
                    Displays (manifest, matrix) pass nothing and use the names.

    Returns::

        {state, runnable, reason, detail, proven, required, missing, unexpected}

    ``reason`` is a stable code; ``detail.note`` is the sentence an operator reads.
    Nothing here carries a secret — only slot NAMES.
    """
    env = environment or {}
    present = bool((card or {}).get("present"))
    if live_slot_values is not None:
        # Only slots with a non-blank value count. An empty secret would be typed
        # into the field and the login would fail as though the app were broken.
        held = [str(k) for k, v in live_slot_values.items()
                if str(k).strip() and str("" if v is None else v).strip()]
        present = present or bool(live_slot_values)
    else:
        held = list((card or {}).get("slot_names") or [])

    # ── A legacy form-login is its own login. ────────────────────────────────
    # Judged FIRST, before the recipe questions: these estates predate recipes and
    # slots entirely, so asking "which recipe slots does this card cover?" has no
    # answer and every no-recipe check below would refuse a configuration that works
    # today. The interpreter's own form-login guard still applies to it.
    if legacy_login_available:
        return _out(UNPROVEN, True, "legacy_form_login",
                    {"required": card_contract.required_slots(recipe)},
                    note=("This member runs on the application's stored form login "
                          "rather than a credential card. Nothing has proven it "
                          "against the recorded login."))

    # ── Nothing to log in with. ──────────────────────────────────────────────
    # Not a card problem, and saying "no card" here would send the operator to the
    # wrong screen.
    if not recipe:
        return _out(NO_RECIPE, False, "no_recipe", {},
                    note=("No login has been recorded for this application, so there "
                          "is nothing for a member to replay. Record the login first."))

    required = card_contract.required_slots(recipe)
    if not required:
        return _out(NO_RECIPE, False, "recipe_has_no_slots", {},
                    note=("The recorded login fills no fields, so it cannot be driven "
                          "by a credential card. Re-record the login."))

    # ── No card. ─────────────────────────────────────────────────────────────
    if not present:
        return _out(NO_CARD, False, "no_card", {"required": required},
                    note=("This member has no credential card for this environment, so "
                          "the login cannot be performed. Running anyway would execute "
                          "the suite logged out and attribute every failure to the "
                          "application. Provision a card with " + ", ".join(required) + "."))

    # ── The card cannot fill the login. THE BLOCK. ───────────────────────────
    # Only a MISSING slot blocks. An extra one cannot: the interpreter reads the
    # slots the recipe declares and ignores any other value, so a leftover from a
    # previous recording still logs in perfectly. Blocking on it would refuse every
    # card in the fleet the moment a login DROPPED a field — work that would have
    # succeeded, stopped for a cosmetic mismatch. (The write-time contract is
    # stricter on purpose: at provisioning time an unexpected name means the
    # operator is filling a different login, and there is no cost to saying so.)
    diff = _slot_diff(required, held, card_contract.optional_slots(recipe))
    if diff["missing"]:
        return _out(STALE_SLOTS, False, "card_slots_do_not_match_recipe", diff,
                    note=("The recorded login was changed after this card was "
                          "provisioned: it no longer supplies " +
                          ", ".join(diff["missing"]) + ". The card cannot perform this "
                          "login. This run is BLOCKED rather than run logged out — a "
                          "skipped login would execute the whole suite unauthenticated "
                          "and attribute every failure to the application. Re-enter this "
                          "member's credentials for the current login (" +
                          ", ".join(required) + ")."))

    # ── The card fits. Is the proof still good? ──────────────────────────────
    status = str((card or {}).get("verify_status") or "unverified")
    card_version = int((card or {}).get("recipe_version") or 0)
    active_version = int((recipe or {}).get("version") or 0)
    card_epoch = str((card or {}).get("verified_epoch") or "")
    env_epoch = str(env.get("data_epoch") or "")
    detail = dict(diff, recipe_version=active_version, card_recipe_version=card_version)

    if diff["unexpected"]:
        # Runnable, but say so: it is evidence the login changed, and the operator
        # should tidy the card before the next re-record removes something that
        # DOES matter.
        return _out(UNPROVEN, True, "card_has_extra_slots", detail,
                    note=("This card still supplies " + ", ".join(diff["unexpected"]) +
                          ", which the recorded login no longer asks for. The login "
                          "still works — the extra value is simply ignored — but the "
                          "card is out of step with the recording and is not treated "
                          "as proven."))

    if status == "failed":
        return _out(UNPROVEN, True, "proof_failed", detail,
                    note=("The last attempt to log in with this card did not reach the "
                          "recorded landing page. The credentials may be wrong, or the "
                          "application may have changed. Verify the card."))

    if status != "verified":
        return _out(UNPROVEN, True, "never_proven", detail,
                    note=("This card has never been used to complete a real login, so "
                          "nothing here claims it works. The next run — or Verify — "
                          "will prove it."))

    # Proven, but against WHAT? A version stamp of 0 means the card predates the
    # contract; it was never checked against any recipe, so its 'verified' cannot be
    # attributed to the current one.
    if card_version == 0:
        return _out(UNPROVEN, True, "proof_predates_contract", detail,
                    note=("This card was proven before cards recorded which login they "
                          "were checked against, so the proof cannot be tied to the "
                          "current recorded login. Verify it again."))

    if active_version and card_version != active_version:
        return _out(UNPROVEN, True, "proof_superseded", detail,
                    note=("This card was proven against login version %d, and version %d "
                          "is now active. The slots still match, so the run may proceed — "
                          "but the earlier proof is not evidence about the current login."
                          % (card_version, active_version)))

    if env_epoch and card_epoch != env_epoch:
        return _out(UNPROVEN, True, "proof_stale_epoch",
                    dict(detail, verified_epoch=card_epoch, data_epoch=env_epoch),
                    note=("This environment's data has been refreshed (epoch %s) since "
                          "this card was proven (epoch %s). The account may no longer "
                          "exist in the new snapshot." % (env_epoch or "-", card_epoch or "-")))

    return _out(READY, True, "", detail,
                note="Proven against the current login and this environment's data.")


def _out(state: str, runnable: bool, reason: str, detail: dict, *, note: str) -> dict:
    return {
        "state": state,
        "runnable": bool(runnable) and state in _RUNNABLE,
        "proven": state == READY,
        "reason": reason,
        "required": list(detail.get("required") or []),
        "missing": list(detail.get("missing") or []),
        "unexpected": list(detail.get("unexpected") or []),
        "detail": dict(detail, note=note),
    }

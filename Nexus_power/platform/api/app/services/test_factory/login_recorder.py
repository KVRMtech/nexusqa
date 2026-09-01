"""Crawl-recorder core (Phase 3/5) — turn an OBSERVED login into a recipe + reuse key.

When the crawler logs in during a crawl it observes: the login path, the fields it
filled (each tied to a slot), the submit control, any verify-documents interstitial
it had to clear, and the landing (Home). This module deterministically converts that
single observation into

  - a persisted-ready login RECIPE (steps + slots) in the interpreter's replay shape
    (Member -> [Verify-documents, optional] -> [Home oracle]), via
    ``persona_store.build_login_recipe``; and
  - the ``login_type_key`` that keys the recipe for fleet-wide reuse, via
    ``login_fingerprint.login_type_key``.

Both are derived from the SAME observed field identifiers, so the key stamped at
record time equals the key computed at match time from another app's identical login
form — which is what makes "record once, reuse fleet-wide" actually match. Pure — no
DB, no I/O; the crawler supplies the observation, this returns the recipe to save.
"""
from __future__ import annotations

import hashlib
import json

from . import persona_store
from . import login_fingerprint


def _recipe_from_sequence(login_path: str, sequence: list, fields: list) -> tuple:
    """Replay steps in the OBSERVED order, plus the slot list the card must fill.

    Emits the interpreter's shape — ``goto`` the login page, then each observed
    ``fill``/``click`` verbatim in order, then settle.

    Every slot is typed ``secret``, matching the legacy form-login path. That is
    deliberate: the interpreter's missing-credential guard EXEMPTS non-secret
    slots, so typing an observed member number ``plain`` would let a run whose
    card is missing that value proceed and pass — a green wash. The observed
    masking hint is not worth that hole; a card encrypts every slot regardless."""
    steps: list = [{"action": "goto", "path": str(login_path or "/")}]
    slots: list = []
    seen: set = set()
    for raw in sequence:
        step = dict(raw or {})
        action = step.get("action")
        if action == "fill":
            slot = str(step.get("slot") or "").strip()
            if not slot:
                continue
            steps.append({"action": "fill", "slot": slot,
                          "label": str(step.get("label") or slot)})
            if slot not in seen:
                seen.add(slot)
                slots.append({"name": slot, "type": "secret"})
        elif action == "click":
            name = str(step.get("name") or "").strip()
            if name:
                click: dict = {"action": "click", "name": name}
                role = str(step.get("role") or "").strip()
                if role:
                    click["role"] = role      # an anchor rung is not role=button
                steps.append(click)
    steps.append({"action": "wait", "state": "networkidle"})
    return steps, slots


def recipe_from_observed_login(observation: dict) -> dict:
    """Deterministically build ``{steps, slots, login_type_key}`` from a crawler's
    login observation.

    ``observation`` shape (the crawler's contract):
      - ``domain``:        registrable host of the app (crawler-computed)
      - ``login_path``:    the login page path
      - ``fields``:        [{``slot``: str, ``label``?: str, ``type``?: str}] — the
                           identity/credential fields filled, in order
      - ``submit``:        the submit control's accessible name
      - ``verify_documents``? : [ {action, ...} ] — interstitial steps observed
                                between submit and Home (marked OPTIONAL by the
                                builder, so members without the document skip them)
      - ``home``? :        {``url_pattern`` | ``selector`` | ``expect_text``} — the
                           logged-in landing, for the Home-reached oracle

    The slot NAME used in the recipe and the field NAME used in the fingerprint are
    the SAME observed identifier (``slot``), keeping record-time and match-time keys
    consistent."""
    obs = observation or {}
    fields = list(obs.get("fields") or [])
    login_path = str(obs.get("login_path") or "/")
    sequence = list(obs.get("sequence") or [])

    if sequence:
        # ORDERED path — the observation carries the TRUE interleaving of fills and
        # control presses, so a multi-step ladder (member -> Continue -> PIN ->
        # Verify) replays exactly as performed. The flat path below cannot express
        # that: it emits every fill, then a single submit.
        steps, slots = _recipe_from_sequence(login_path, sequence, fields)
        vdoc = persona_store._verify_document_steps(obs.get("verify_documents"))
        if vdoc:
            steps = steps + vdoc
        home_step = persona_store._assert_home_step(obs.get("home"))
        if home_step:
            steps = steps + [home_step]
    else:
        # Shape the observed fields into the form-login cfg build_login_recipe
        # consumes (its slot name = the field's ``value``).
        cfg = {
            "login_path": login_path,
            "fields": [{"value": f.get("slot"),
                        "label": f.get("label") or f.get("slot")}
                       for f in fields if (f or {}).get("slot")],
            "submit_label": obs.get("submit") or "Sign in",
        }
        steps, slots = persona_store.build_login_recipe(
            cfg,
            verify_documents=obs.get("verify_documents"),
            home=obs.get("home"),
        )

    key = login_fingerprint.login_type_key(
        domain=obs.get("domain") or "",
        login_path=login_path,
        fields=[{"name": f.get("slot"), "type": f.get("type") or "text"}
                for f in fields if (f or {}).get("slot")],
        submit=obs.get("submit") or "",
    )
    return {"steps": steps, "slots": slots, "login_type_key": key}


def _macro_key(name: object, steps: list) -> str:
    """Stable, value-free id for a recorded macro — its human name + step SHAPE
    (actions + slot/name/path, never values). Lets a macro be stored + looked up."""
    shape = [[str(s.get("action") or ""),
              str(s.get("slot") or s.get("name") or s.get("path") or "")]
             for s in steps if isinstance(s, dict)]
    basis = str(name or "") + "|" + json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return "macro:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def recipe_from_observed_macro(observation: dict) -> dict:
    """Build a domain-agnostic REPLAYABLE MACRO from an observed interaction — the
    record-once mechanism generalized beyond login (U4).

    Unlike :func:`recipe_from_observed_login` this has NO login-shaped tail
    (verify-documents / Home assertion / login_type_key): a macro is ANY widget or
    flow a human demonstrated once (a signature pad, an exotic date grid, a vendor
    checkout). It reuses the SAME generic sequence→steps builder, so the interpreter
    replays it exactly as performed; every filled slot is a ``secret`` card slot so
    a missing value can never green-wash a run.

    ``observation`` shape:
      - ``start_path``:  where the macro begins (path); ``login_path`` accepted too
      - ``sequence``:    ``[{action: fill|click, slot?/label?/name?/role?}]`` in order
      - ``name``?:       a human label for the macro
    Returns ``{steps, slots, macro_key}``.
    """
    obs = observation or {}
    start_path = str(obs.get("start_path") or obs.get("login_path") or "/")
    sequence = list(obs.get("sequence") or [])
    steps, slots = _recipe_from_sequence(start_path, sequence, list(obs.get("fields") or []))
    return {"steps": steps, "slots": slots, "macro_key": _macro_key(obs.get("name"), steps)}


__all__ = ["recipe_from_observed_login", "recipe_from_observed_macro"]

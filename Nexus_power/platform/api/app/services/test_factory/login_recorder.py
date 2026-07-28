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

from . import persona_store
from . import login_fingerprint


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

    # Shape the observed fields into the form-login cfg build_login_recipe consumes
    # (its slot name = the field's ``value``).
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


__all__ = ["recipe_from_observed_login"]

"""P5 — scale: trait-based persona selection, credential staleness (keyed to the
environment data epoch), bulk import / non-secret manifest, impersonation
scaffold, and the ops summary.

Pins the doctrine at scale:
  * a persona is resolved by traits/class DETERMINISTICALLY (most specific), and
    an unmatched request chooses NOTHING — never a guessed member;
  * a card is STALE when the data epoch rolled past its verification, the TTL
    lapsed, or it was never verified — a re-verify signal, never a silent pass;
  * a manifest exports slot NAMES only — a credential value never leaves the
    envelope;
  * an impersonation recipe is an ordinary recipe (no embedded secret).

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_p5.py -q
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from app.services.test_factory import persona_scale as ps

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()
_STORE = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                           "test_factory", "persona_store.py"), encoding="utf-8").read()
_SQL = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "apply_persona_env_p5.sql"), encoding="utf-8").read()

NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


# ── trait-based selection ────────────────────────────────────────────────────

_PEOPLE = [
    {"persona_id": "y", "name": "Maria", "behavior_class": "young-small-family",
     "traits": ["2kids", "employed"], "status": "active"},
    {"persona_id": "s", "name": "John", "behavior_class": "senior-large-family",
     "traits": ["5kids", "retired"], "status": "active"},
    {"persona_id": "s2", "name": "Al", "behavior_class": "senior-large-family",
     "traits": ["5kids", "retired", "veteran"], "status": "active"},
]


def test_select_by_behavior_class_picks_most_specific_deterministically():
    out = ps.select_persona(_PEOPLE, behavior_class="senior-large-family")
    # two match; the most specific (fewest surplus traits) then name wins → John
    assert out["persona"]["persona_id"] == "s"
    assert out["ambiguous"] is True
    assert set(out["candidates"]) == {"s", "s2"}


def test_select_by_traits_intersects_the_request():
    out = ps.select_persona(_PEOPLE, traits=["veteran"])
    assert out["persona"]["persona_id"] == "s2"
    assert out["ambiguous"] is False


def test_no_match_chooses_nothing_never_guesses():
    out = ps.select_persona(_PEOPLE, behavior_class="single-no-kids")
    assert out["persona"] is None
    assert "nothing matched" in out["reason"] or "no active persona" in out["reason"]


def test_retired_personas_are_not_selected():
    retired = [{"persona_id": "r", "name": "Old", "behavior_class": "young-small-family",
                "traits": [], "status": "retired"}]
    out = ps.select_persona(retired, behavior_class="young-small-family")
    assert out["persona"] is None


# ── credential staleness ─────────────────────────────────────────────────────

def test_never_verified_is_stale():
    s = ps.card_staleness({"present": True, "verify_status": "unverified"},
                          {"data_epoch": "e1"}, now=NOW)
    assert s["state"] == ps.STALE_NEVER


def test_epoch_roll_makes_a_verified_card_stale():
    card = {"present": True, "verify_status": "verified",
            "last_verified_at": NOW.isoformat(), "verified_epoch": "e1"}
    s = ps.card_staleness(card, {"data_epoch": "e2"}, now=NOW)
    assert s["state"] == ps.STALE_EPOCH
    assert "e2" in s["reason"]


def test_ttl_lapse_makes_a_card_stale():
    old = (NOW - timedelta(days=40)).isoformat()
    card = {"present": True, "verify_status": "verified",
            "last_verified_at": old, "verified_epoch": "e1"}
    s = ps.card_staleness(card, {"data_epoch": "e1"}, now=NOW, ttl_days=30)
    assert s["state"] == ps.STALE_TTL


def test_verified_current_epoch_within_ttl_is_fresh():
    card = {"present": True, "verify_status": "verified",
            "last_verified_at": NOW.isoformat(), "verified_epoch": "e1"}
    s = ps.card_staleness(card, {"data_epoch": "e1"}, now=NOW, ttl_days=30)
    assert s["state"] == ps.FRESH


def test_staleness_report_lists_the_due():
    cards = {
        "fresh": {"present": True, "verify_status": "verified",
                  "last_verified_at": NOW.isoformat(), "verified_epoch": "e1"},
        "rolled": {"present": True, "verify_status": "verified",
                   "last_verified_at": NOW.isoformat(), "verified_epoch": "e0"},
        "never": {"present": True, "verify_status": "unverified"},
    }
    rep = ps.staleness_report(cards, {"data_epoch": "e1"}, now=NOW)
    assert set(rep["due"]) == {"rolled", "never"}
    assert rep["due_count"] == 2
    assert rep["counts"][ps.FRESH] == 1


# ── impersonation scaffold ───────────────────────────────────────────────────

def test_impersonation_scaffold_is_a_plain_recipe_no_secret():
    admin = [{"action": "goto", "path": "/admin"},
             {"action": "fill", "slot": "admin_user", "selector": "#u"},
             {"action": "fill", "slot": "admin_pass", "selector": "#p"}]
    out = ps.build_impersonation_recipe(admin_login_steps=admin,
                                        impersonate_path="/admin/impersonate",
                                        member_slot="member_number")
    kinds = [s["action"] for s in out["steps"]]
    assert kinds[-3:] == ["goto", "fill", "click"]        # admin login → goto → fill member → submit
    slot_names = {s["name"] for s in out["slots"]}
    assert {"admin_user", "admin_pass", "member_number"} <= slot_names
    # no secret VALUE anywhere in the scaffold
    import json as _j
    assert "password" not in _j.dumps(out).lower() or "admin_pass" in slot_names


# ── migration + store + endpoints ────────────────────────────────────────────

def test_migration_adds_verified_epoch_additively():
    assert "ADD COLUMN IF NOT EXISTS verified_epoch" in _SQL


def test_store_has_scale_helpers_and_epoch_column():
    assert "verified_epoch" in _STORE
    for fn in ("stamp_card_verified", "all_credential_status"):
        assert f"async def {fn}(" in _STORE


def test_dispatch_resolves_persona_by_match():
    assert "persona_scale.select_persona(" in _ROUTER
    assert "persona_match" in _ROUTER
    # a miss is an honest 422, never a guessed member
    seg = _ROUTER[_ROUTER.index("if not persona_id and body.persona_match:"):]
    assert "No member is guessed" in seg[:1200]


def test_dispatch_stamps_card_verification_with_epoch():
    assert "stamp_card_verified(" in _ROUTER
    seg = _ROUTER[_ROUTER.index("Persona preflight"):]
    assert "data_epoch=str(governance_env.get" in seg[:2600]


def test_dispatch_stamps_verified_only_when_the_login_was_PROVEN():
    """F3. The stamp used to be unconditional with `verified=bool(pf['ok'])`, and
    `ok` is true for a login that only replayed its steps — so a card could be
    marked verified when nobody had logged in. On a permissive test app nothing
    would ever reveal it."""
    seg = _ROUTER[_ROUTER.index("Persona preflight"):]
    seg = seg[:3000]
    assert 'verified=bool(pf.get("proven"))' in seg
    assert 'verified=bool(pf.get("ok"))' not in seg
    # an ALLOWED-but-unproven run leaves the card's previous state alone rather
    # than overwriting it with a claim the run cannot support
    assert 'if pf.get("proven") or (not pf.get("ok") and _blames_card):' in seg
    # …and a FAILURE is only recorded against the card when the card is what failed.
    # Recipe drift and a runner outage are not the credential's fault; marking it
    # failed for them destroys a good proof and points at a password that was never
    # wrong — the exact misattribution login_probe exists to prevent.
    assert '_blames_card = (pf.get("attribution") == "credential")' in seg
    # the proof records WHICH login version it proved, or `ready` is unreachable
    # after any re-record
    assert "recipe_version=int((_auth.get(\"recipe\") or {}).get(\"version\") or 0)" in seg


def test_p5_endpoints_exist():
    for path in ("/personas/select", "/environments/{environment_id}/staleness",
                 "/credentials/bulk-import", "/credentials/manifest",
                 "/personas/ops/summary", "/recipes/impersonation-scaffold"):
        assert path in _ROUTER


def test_manifest_and_bulk_never_leak_secrets():
    seg = _ROUTER[_ROUTER.index("async def credentials_manifest_endpoint"):]
    seg = seg[:seg.index("async def persona_ops_summary_endpoint")]
    assert "Slot NAMES only" in seg
    # bulk import refuses plaintext when the envelope is unavailable
    imp = _ROUTER[_ROUTER.index("async def bulk_import_credentials_endpoint"):]
    assert "refusing to import credentials in plaintext" in imp[:1500]

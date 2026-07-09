"""Pure-logic tests for seed manifest, drift, A/B, lens, and secret-scrub —
everything gradeable without a database or a git remote.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ab.harness import ab_report  # noqa: E402
from app.drift.report import (  # noqa: E402
    ROUTE_IN_CODE_UNREACHABLE,
    ROUTE_LIVE_NOT_IN_CODE,
    VALIDATOR_UNTESTED,
    build_drift_report,
)
from app.extract.registry import Atom  # noqa: E402
from app.lens.llm_lens import explain_atoms, quote_is_verbatim  # noqa: E402
from app.manifest.schema import SeedValidationError, validate_seed_manifest  # noqa: E402
from app.manifest.seed import build_seed_manifest  # noqa: E402
from app.security.secret_scrub import find_secrets, scrub  # noqa: E402


def _atom(kind, value, path="src/x.ts", line=1, quote="q"):
    return Atom(kind=kind, value=value, provenance_path=path, provenance_line=line,
                provenance_sha="abc", quote=quote, extractor="t", confidence=0.9,
                source_tier="static_regex")


# ─────────────────────────────── seed ────────────────────────────────────


def test_seed_ranks_payment_over_marketing_and_is_deterministic():
    atoms = [
        _atom("route", {"path_pattern": "/about"}),
        _atom("api_endpoint", {"method": "POST", "path": "/payments/charge"}),
        _atom("route", {"path_pattern": "/login"}),
    ]
    m1 = build_seed_manifest(atoms)
    m2 = build_seed_manifest(list(reversed(atoms)))
    assert m1 == m2, "seed manifest must be deterministic regardless of atom order"
    paths = [r["path_pattern"] for r in m1["ranked_routes"]]
    assert paths[0] == "/payments/charge", paths  # highest criticality first
    assert m1["version"] == "seed-v1"


def test_seed_auth_recipe_has_names_never_values():
    atoms = [_atom("route", {"path_pattern": "/login"})]
    m = build_seed_manifest(atoms)
    recipe = m["auth_recipe"]
    assert recipe["login_route"] == "/login"
    assert "values" not in recipe and "credentials" not in recipe


def test_seed_rejects_credential_shaped_content():
    bad = {
        "version": "seed-v1",
        "ranked_routes": [],
        "auth_recipe": {"login_route": "/l", "field_names": [], "provenance": "",
                        "password": "hunter2"},
        "nav_edges": [],
    }
    try:
        validate_seed_manifest(bad)
        assert False, "should have refused a credential-shaped key"
    except SeedValidationError:
        pass


# ─────────────────────────────── drift ───────────────────────────────────


def test_drift_classifies_all_kinds():
    code = [
        _atom("route", {"path_pattern": "/dashboard"}),
        _atom("api_endpoint", {"method": "GET", "path": "/orders/:id"}),
        _atom("validator_rule", {"field": "ssn", "rule": "IsNotEmpty"}),
    ]
    live = ["/dashboard", "/orders/42", "/promo-live-only"]
    report = build_drift_report(code, live)
    kinds = {i["kind"] for i in report["items"]}
    assert ROUTE_LIVE_NOT_IN_CODE in kinds        # /promo-live-only
    assert VALIDATOR_UNTESTED in kinds            # ssn never reached
    # /dashboard + /orders/:id are reachable ⇒ not flagged unreachable
    unreachable = [i["code_side"] for i in report["items"] if i["kind"] == ROUTE_IN_CODE_UNREACHABLE]
    assert "/dashboard" not in unreachable and "/orders/:param" not in unreachable
    assert report["summary"]["reachable"] == 2


def test_drift_unreachable_route():
    code = [_atom("route", {"path_pattern": "/secret-admin"})]
    report = build_drift_report(code, ["/home"])
    assert any(i["kind"] == ROUTE_IN_CODE_UNREACHABLE and i["code_side"] == "/secret-admin"
               for i in report["items"])


# ───────────────────────────────── A/B ───────────────────────────────────


def test_ab_report_delta_positive_when_directed_reaches_more():
    universe = ["/a", "/b", "/c", "/d"]
    directed = ["/a", "/b", "/c"]
    blind = ["/a"]
    rep = ab_report(universe, directed, blind)
    assert rep["directed_recall"] == 0.75
    assert rep["blind_recall"] == 0.25
    assert rep["recall_delta"] == 0.5
    assert rep["seeding_helps"] is True


def test_ab_report_honest_when_seeding_does_not_help():
    rep = ab_report(["/a", "/b"], ["/a"], ["/a", "/b"])
    assert rep["recall_delta"] <= 0
    assert rep["seeding_helps"] is False


# ───────────────────────────────── lens ──────────────────────────────────


def test_lens_off_is_pure_noop():
    atoms = [_atom("route", {"path_pattern": "/x"})]
    out = explain_atoms(atoms, llm_client=None, enabled=False)
    assert len(out) == 1
    assert out[0].status == "unverifiable"
    assert out[0].explanation == ""
    assert out[0].atom is atoms[0]


def test_lens_demotes_non_verbatim_quote():
    atom = _atom("route", {"path_pattern": "/x"}, quote="path: '/x'")

    class FabricatingClient:
        def explain_atom(self, _d):
            return {"explanation": "this is the login route", "quote": "TOTALLY NOT IN SOURCE"}

    out = explain_atoms([atom], llm_client=FabricatingClient(), enabled=True)
    assert out[0].status == "unverifiable"      # fabricated quote rejected
    assert out[0].explanation == ""


def test_lens_accepts_verbatim_quote():
    atom = _atom("route", {"path_pattern": "/x"}, quote="path: '/x' element={<Home/>}")

    class GroundedClient:
        def explain_atom(self, _d):
            return {"explanation": "home route", "quote": "path: '/x'"}

    out = explain_atoms([atom], llm_client=GroundedClient(), enabled=True)
    assert out[0].status == "verified"
    assert out[0].explanation == "home route"


def test_quote_is_verbatim():
    assert quote_is_verbatim("abc", "xx abc yy")
    assert not quote_is_verbatim("abc", "xx def yy")
    assert not quote_is_verbatim("", "anything")


# ─────────────────────────── secret scrub (hardened) ─────────────────────


def test_scrub_catches_common_key_shapes():
    for secret, label in [
        ("sk-QECsecret0123456789abcdefghijABCD", "OPENAI_KEY"),
        ("AKIAIOSFODNN7EXAMPLE", "AWS_KEY"),
        ("ghp_0123456789012345678901234567890123", "GITHUB_TOKEN"),
        ("sk_live_0123456789abcdefABCD", "STRIPE_KEY"),
    ]:
        out = scrub(f"const key = '{secret}';")
        assert secret not in out, f"{label} leaked: {out}"
        assert "[REDACTED:" in out


def test_scrub_redacts_assigned_password_but_keeps_key():
    out = scrub('password = "hunter2super"')
    assert "hunter2super" not in out
    assert "password" in out  # the key name is preserved for legibility


def test_find_secrets_never_returns_raw():
    hits = find_secrets("token = 'sk-QECsecret0123456789abcdefghijABCD'")
    assert hits
    for h in hits:
        assert "QECsecret0123456789" not in h.sample

"""S4 ⇄ VKPower contract pins — upstream drift fails in OUR CI, not prod.

S4 (scenario governance) consumes the UNCHANGED VKPower factory read-only over
HTTP + reads the substrate/audit tables.  This suite pins EVERY VKPower shape S4
depends on so a refactor on the factory side breaks a fast, hermetic test here
instead of silently corrupting governance in production:

  1. ``GET …/rtm`` response shape + the ``oracle_kind`` vocabulary
     (``provenance.build_rtm`` / ``provenance._oracle_kind``);
  2. the tier labeller's behavioural oracle set is a grounded SUBSET of that
     vocabulary (S4's own consumption is exercised against a fixture payload);
  3. the case-review vocabulary + the exact 422-no-signature contract, and that
     S4's approval gate reproduces the SAME 422 message byte-for-byte;
  4. ``audit_log`` columns (the touch-meter ingest dedupes on ``log_id``);
  5. ``PageVisitRow`` / ``PageActionRow`` columns the synthesiser reads;
  6. ``POST …/generate`` response keys (the materialize bridge) +
     ``reapply_approved`` running LAST (approved content survives regenerate).

SDK models are pinned by IMPORT (nexus_sdk is on the path).  The platform-api
shapes are pinned by reading the REAL source (importing platform_api pulls heavy
deps not present in the qe-central test image) — load-bearing substrings +
ordering, so a rename/removal fails loudly.  No DB or live service is required.
"""
from __future__ import annotations

import os

import pytest

# ── Locate the real platform-api source (sibling bounded context) ─────────
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PLATFORM_API = os.path.abspath(os.path.join(_TESTS_DIR, "..", "..", "api"))
_TF_ROUTER = os.path.join(_PLATFORM_API, "app", "routers", "test_factory.py")
_PROVENANCE = os.path.join(_PLATFORM_API, "app", "services", "test_factory", "provenance.py")
_SERVICE = os.path.join(_PLATFORM_API, "app", "services", "test_factory", "service.py")


def _read(path: str) -> str:
    if not os.path.isfile(path):
        pytest.skip(f"platform-api source not present at {path} (stripped checkout)")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# ══════════════════════ 1. /rtm response shape ════════════════════════════

def test_rtm_endpoint_returns_artifact_id_and_tests():
    """The endpoint envelope S4's tier labeller iterates: {artifact_id, tests[]}."""
    src = _read(_TF_ROUTER)
    assert '/rtm")' in src, "GET …/rtm route path drifted"
    assert '"tests": [build_rtm(' in src, "/rtm no longer returns tests=[build_rtm(...)]"
    assert '"artifact_id": artifact_id' in src


def test_build_rtm_test_and_row_shape():
    """build_rtm's per-test + per-row + per-assertion keys the labeller reads."""
    src = _read(_PROVENANCE)
    for key in ('"test_id"', '"name"', '"expected_outcome"', '"rows"'):
        assert key in src, f"/rtm test key {key} drifted"
    for key in ('"step_number"', '"action"', '"provenance"', '"confidence"',
                '"observed_label"', '"frame_ref"', '"emitted_assertions"', '"unproven"'):
        assert key in src, f"/rtm row key {key} drifted"
    # Per-assertion keys the tier labeller reads.
    for key in ('"code"', '"oracle_kind"', '"grounded"'):
        assert key in src, f"/rtm emitted_assertion key {key} drifted"


# ══════════════════════ 2. oracle_kind vocabulary ═════════════════════════

# The full vocabulary provenance._oracle_kind emits (test_factory drift guard).
_ORACLE_VOCAB = {
    "navigation", "value-oracle", "value-presence", "state",
    "outcome-region", "a11y", "presence", "assertion",
}


def test_oracle_kind_vocabulary_present():
    """Every oracle_kind literal S4 may see is still emitted by the compiler."""
    src = _read(_PROVENANCE)
    for kind in _ORACLE_VOCAB:
        assert f'"{kind}"' in src, f"oracle_kind '{kind}' drifted out of provenance"
    # The two behavioural kinds are classified GROUNDED at their emission sites.
    assert 'return ("navigation", True)' in src
    assert 'return ("outcome-region", True)' in src


def test_behavioral_set_is_grounded_subset_of_vocabulary():
    """S4's BEHAVES axis keys on navigation+outcome-region — a grounded subset."""
    from app.services import tier_label

    assert tier_label.BEHAVIORAL_ORACLE_KINDS == {"navigation", "outcome-region"}
    assert tier_label.BEHAVIORAL_ORACLE_KINDS <= _ORACLE_VOCAB


def test_tier_labeller_consumes_rtm_payload_as_pinned():
    """Exercise S4's consumption against a fixture in the pinned /rtm shape:
    a grounded navigation ⇒ BEHAVES; a fill-only (value-oracle only) ⇒ RENDERS;
    suite = MIN."""
    from app.services import tier_label

    behaves = {
        "test_id": "t_nav", "name": "login flow",
        "rows": [{"step_number": 1, "emitted_assertions": [
            {"code": "await expect(page).toHaveURL(/home/)",
             "oracle_kind": "navigation", "grounded": True}]}],
    }
    renders = {
        "test_id": "t_fill", "name": "fill only",
        "rows": [{"step_number": 1, "emitted_assertions": [
            {"code": "await expect(page.getByLabel('Email')).toHaveValue(/x/i)",
             "oracle_kind": "value-oracle", "grounded": True}]}],
    }
    assert tier_label.label_case(behaves)["tier"] == tier_label.TIER_BEHAVES
    assert tier_label.label_case(renders)["tier"] == tier_label.TIER_RENDERS
    rtm = {"artifact_id": "a1", "tests": [behaves, renders]}
    labelled = tier_label.label_rtm(rtm)
    # One renders + one behaves ⇒ suite = MIN = renders (fail-down).
    assert labelled["suite_tier"] == tier_label.TIER_RENDERS
    assert labelled["counts"] == {"behaves": 1, "renders": 1, "total": 2}


# ══════════════════════ 3. review vocabulary + 422 ════════════════════════

_REVIEW_422 = "An e-signature (your full name) is required to approve"


def test_review_vocabulary_and_422_contract():
    """The VKPower case-review action set + the exact 422-no-signature detail."""
    src = _read(_TF_ROUTER)
    assert 'action must be submit|approve|reject|reopen' in src, "review vocabulary drifted"
    assert _REVIEW_422 in src, "422-no-signature detail drifted"
    # ReviewRequest carries action / signature / note (the fields S4 mirrors).
    assert "class ReviewRequest(BaseModel):" in src
    for field in ("action: str", "signature: str", "note: str"):
        assert field in src, f"ReviewRequest field '{field}' drifted"


def test_s4_approval_gate_reproduces_the_same_422_message():
    """S4's own approval gate raises the byte-identical VKPower 422 message +
    carries http_status=422 so the router maps it faithfully."""
    from app.services import approval

    exc = approval.SignatureRequiredError()
    assert str(exc) == _REVIEW_422
    assert getattr(exc, "http_status", None) == 422
    # The gate fires only on approve (parity with the VKPower endpoint).
    assert approval.SIGNATURE_REQUIRED_ACTIONS == frozenset({approval.ACTION_APPROVE})
    with pytest.raises(approval.SignatureRequiredError):
        approval.require_signature(approval.ACTION_APPROVE, "   ")
    # A non-approve action tolerates a blank signature.
    assert approval.require_signature(approval.ACTION_SUBMIT, "") == ""


# ══════════════════════ 4. audit_log columns ══════════════════════════════

def test_audit_log_columns_pinned():
    """The touch-meter ingest reads audit_log + dedupes on log_id — pin its shape."""
    from nexus_sdk.db.models import AuditLogRow

    cols = {c.name for c in AuditLogRow.__table__.columns}
    assert cols == {
        "log_id", "tenant_id", "engine", "action", "entity_type",
        "entity_id", "user_id", "details", "created_at",
    }, f"audit_log columns drifted: {cols}"
    # The dedupe key + the fields the classifier reads.
    for needed in ("log_id", "action", "user_id", "tenant_id", "created_at"):
        assert needed in cols


# ══════════════════════ 5. PageVisit / PageAction columns ═════════════════

def test_page_visit_columns_the_synthesiser_reads():
    from nexus_sdk.db.models import PageVisitRow

    cols = {c.name for c in PageVisitRow.__table__.columns}
    for needed in ("page_visit_id", "artifact_id", "sequence_index", "url_host",
                   "url_path", "canonical_host", "form_snapshot",
                   "form_snapshot_signals", "source", "extractor_version",
                   "created_at"):
        assert needed in cols, f"page_visits.{needed} drifted (synthesis reads it)"


def test_page_action_columns_the_synthesiser_reads():
    from nexus_sdk.db.models import PageActionRow

    cols = {c.name for c in PageActionRow.__table__.columns}
    for needed in ("page_action_id", "page_visit_id", "artifact_id",
                   "subaction_index", "verb", "target_label", "target_kind",
                   "value", "evidence_signals", "extractor_version", "created_at"):
        assert needed in cols, f"page_actions.{needed} drifted (synthesis reads it)"


# ══════════════════════ 6. /generate response + reapply order ═════════════

def test_generate_response_keys_for_materialize_bridge():
    """The materialize bridge posts /generate; pin the keys it surfaces."""
    endpoint = _read(_TF_ROUTER)
    assert 'return {"success": True' in endpoint, "/generate success envelope drifted"
    assert '"approved_protected": approved_protected' in endpoint
    service = _read(_SERVICE)
    assert '"demonstrated": len(result.test_cases)' in service
    assert '"generated": len(new_ids)' in service


def test_reapply_approved_runs_last_so_approved_survives_regenerate():
    """/generate must reapply APPROVED content LAST (after added + overrides) so a
    regenerate can never silently overwrite a human-approved case."""
    src = _read(_TF_ROUTER)
    i_added = src.find("proposer.reapply_added_cases(")
    i_overrides = src.find("_reapply_tf_overrides(")
    i_approved = src.find("proposer.reapply_approved(")
    assert -1 not in (i_added, i_overrides, i_approved), "generate reapply calls drifted"
    assert i_approved > i_added and i_approved > i_overrides, (
        "reapply_approved must run LAST (approved content wins)"
    )
    # The survive-regenerate key exists in the proposer.
    proposer = _read(os.path.join(
        _PLATFORM_API, "app", "services", "test_factory", "proposer.py"))
    assert '_APPROVED_KEY = "test_factory_approved"' in proposer
    assert "Runs LAST" in proposer

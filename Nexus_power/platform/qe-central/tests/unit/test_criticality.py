"""QE-Central S4 — criticality registry unit tests (pure, no DB).

Grades :func:`app.services.criticality.evaluate` against HAND-LABELLED
fixtures (the measure-first doctrine — the classifier never grades itself):

  * a money route bands P0;
  * an unknown subject FAILS UP to P1 — NEVER silently P2/P3;
  * the seed pack only ever emits P0/P1 (P2/P3 require an EXPLICIT down-band
    in a custom pack, never omission);
  * matching is precision-aware (``payment`` ≠ ``repayment``) yet fail-up-safe;
  * banding is deterministic and picks the MOST-critical matched band.
"""
from __future__ import annotations

import pytest

from app.services import criticality as C
from app.services.criticality import (
    BAND_P0,
    BAND_P1,
    BAND_P2,
    FAIL_UP_BAND,
    evaluate,
    seed_pack,
    subject_from_journey,
)

# ── Hand-labelled fixtures: (label, subject, expected_band) ───────────────
# Every subject is a value-free field-space projection; the expected band was
# assigned by hand from the design's seed categories (money / auth / PII /
# destructive / multi-page-submit / invariant-link).
HAND_LABELLED: list[tuple[str, dict, str]] = [
    ("money_route_transfer", {"url_path": "/transfer"}, BAND_P0),
    ("money_route_checkout", {"url_path": "/checkout/step-2"}, BAND_P0),
    ("money_route_payment_history", {"url_path": "/payment-history"}, BAND_P0),
    ("money_control_button", {"button_label": "Transfer funds now"}, BAND_P0),
    ("money_control_place_order", {"button_label": "Place order"}, BAND_P0),
    ("pii_ssn_field", {"field_label": "SSN"}, BAND_P0),
    ("pii_card_number", {"field_label": "Card number"}, BAND_P0),
    ("pii_dob", {"field_label": "Date of birth"}, BAND_P0),
    ("destructive_delete", {"button_label": "Delete account"}, BAND_P0),
    ("destructive_route", {"url_path": "/close-account"}, BAND_P0),
    ("auth_login_route", {"url_path": "/login"}, BAND_P0),
    ("auth_password_field", {"field_label": "Password"}, BAND_P0),
    ("auth_otp_field", {"field_label": "One-time code"}, BAND_P0),
    ("invariant_linked", {"invariant_link": "inv-underwriting-ceiling"}, BAND_P0),
    # Multi-page submit token (emitted only for genuine multi-page submissions).
    ("multi_page_submit", {"action_verb": "type select multi_page_submit"}, BAND_P1),
    # Unknowns → FAIL-UP to P1 (never P2/P3).
    ("unknown_marketing", {"url_path": "/about-us", "button_label": "Learn more"}, BAND_P1),
    ("unknown_profile_view", {"url_path": "/profile", "field_label": "Display name"}, BAND_P1),
    ("empty_subject", {}, BAND_P1),
]


@pytest.mark.parametrize("label,subject,expected", HAND_LABELLED, ids=[x[0] for x in HAND_LABELLED])
def test_hand_labelled_precision(label: str, subject: dict, expected: str) -> None:
    result = evaluate(subject)
    assert result["band"] == expected, f"{label}: got {result['band']} want {expected}"


def test_measured_precision_is_100_percent_on_the_key() -> None:
    """Publish the number (measure-first): seed-pack precision vs the key."""
    correct = sum(1 for _, subj, exp in HAND_LABELLED if evaluate(subj)["band"] == exp)
    precision = correct / len(HAND_LABELLED)
    assert precision == 1.0, f"criticality precision {precision:.3f} < 1.0 on the hand key"


def test_money_route_is_p0_with_named_evidence() -> None:
    result = evaluate({"url_path": "/transfer"})
    assert result["band"] == BAND_P0
    ids = [h["signal_id"] for h in result["evidence"]]
    assert "money_route" in ids
    assert result["classifier"] == "criticality-v1-deterministic"


def test_unknown_fails_up_to_p1_never_p2_or_p3() -> None:
    result = evaluate({"url_path": "/newsletter", "button_label": "Subscribe"})
    assert result["band"] == FAIL_UP_BAND == BAND_P1
    assert result["band"] not in (BAND_P2, "P3")
    # The evidence is honest: it names the fail-up, not a fabricated signal.
    assert result["evidence"][0]["signal_id"] == "fail_up_default"
    assert "FAIL-UP" in result["evidence"][0]["rationale"]


def test_seed_pack_never_emits_p2_or_p3() -> None:
    """No omission path can produce P2/P3 — the seed pack bands only P0/P1."""
    probes = [
        {"url_path": "/transfer"}, {"field_label": "Password"},
        {"button_label": "Delete"}, {"url_path": "/dashboard"},
        {"field_label": "First name"}, {}, {"action_verb": "click"},
        {"url_path": "/settings", "field_label": "Theme"},
    ]
    for subject in probes:
        assert evaluate(subject)["band"] in (BAND_P0, BAND_P1)


def test_most_critical_band_wins_when_multiple_signals_match() -> None:
    """Money (P0) + multi-page-submit (P1) → P0 (the MIN band, most critical)."""
    subject = {"url_path": "/transfer", "action_verb": "type multi_page_submit"}
    result = evaluate(subject)
    assert result["band"] == BAND_P0
    matched_ids = {h["signal_id"] for h in result["evidence"]}
    assert {"money_route", "multi_page_submit"} <= matched_ids
    # Evidence is ordered most-critical first.
    assert result["evidence"][0]["band"] == BAND_P0


def test_whole_word_matching_avoids_false_positives() -> None:
    """A bare single-token marker matches a whole word, not a substring."""
    # 'payment' (money_control token) must NOT fire on 'repayment schedule'.
    assert evaluate({"button_label": "Repayment schedule"})["band"] == BAND_P1
    # But a separator-bearing marker '/payment' still matches by substring.
    assert evaluate({"url_path": "/payment-methods"})["band"] == BAND_P0


def test_determinism() -> None:
    subject = {"url_path": "/transfer", "field_label": "Amount"}
    assert evaluate(subject) == evaluate(subject)


def test_explicit_pack_can_downband_to_p2() -> None:
    """P2/P3 are reachable ONLY via an explicit signal band — never fail-up."""
    custom = {
        "registry_version": "custom-vX",
        "signals": [
            {"signal_id": "low_risk_help", "band": BAND_P2,
             "applies_to": "url_path", "matchers": ["/help"], "rationale": "docs"},
        ],
    }
    result = evaluate({"url_path": "/help/faq"}, pack=custom)
    assert result["band"] == BAND_P2
    assert result["registry_version"] == "custom-vX"


def test_malformed_subject_fails_up() -> None:
    assert evaluate(None)["band"] == BAND_P1  # type: ignore[arg-type]
    assert evaluate("not a mapping")["band"] == BAND_P1  # type: ignore[arg-type]


def test_empty_pack_fails_up() -> None:
    result = evaluate({"url_path": "/transfer"}, pack={"signals": []})
    assert result["band"] == BAND_P1
    assert result["evidence"][0]["signal_id"] == "fail_up_default"


def test_invalid_signal_band_fails_up_not_p3() -> None:
    """A signal declaring a nonsense band is normalised to the fail-up band."""
    bad = {"signals": [
        {"signal_id": "typo", "band": "P9", "applies_to": "url_path",
         "matchers": ["/x"], "rationale": "typo band"},
    ]}
    assert evaluate({"url_path": "/x"}, pack=bad)["band"] == BAND_P1


def test_malformed_signal_never_crashes_evaluation() -> None:
    """A junk signal row is skipped; a valid one still fires."""
    pack = {"signals": [
        "not-a-dict",
        {"applies_to": "url_path"},  # no matchers
        {"signal_id": "ok", "band": BAND_P0, "applies_to": "url_path",
         "matchers": ["/transfer"], "rationale": "money"},
    ]}
    result = evaluate({"url_path": "/transfer"}, pack=pack)
    assert result["band"] == BAND_P0


# ── subject_from_journey projection ───────────────────────────────────────

def _journey(pages: list[dict], kind: str = "trunk") -> dict:
    return {"kind": kind, "pages": pages}


def test_subject_from_journey_multi_page_submit_token() -> None:
    """The structural token appears ONLY for a ≥2-page journey with a submit."""
    multi = _journey([
        {"host": "h", "path": "/a", "verbs": ["type"], "fields": ["x"], "targets": ["Next"]},
        {"host": "h", "path": "/b", "verbs": ["submit"], "fields": [], "targets": ["Done"]},
    ])
    subj = subject_from_journey(multi)
    assert C.MULTI_PAGE_SUBMIT_TOKEN in subj["action_verb"]
    assert evaluate(subj)["band"] == BAND_P1

    # Single page + submit → no multi-page token → fail-up P1 (not the signal).
    single = _journey([
        {"host": "h", "path": "/a", "verbs": ["submit"], "fields": [], "targets": ["Done"]},
    ])
    subj1 = subject_from_journey(single)
    assert C.MULTI_PAGE_SUBMIT_TOKEN not in subj1["action_verb"]
    ev = evaluate(subj1)
    assert ev["band"] == BAND_P1
    assert ev["evidence"][0]["signal_id"] == "fail_up_default"


def test_subject_from_journey_invariant_link_presence_bands_p0() -> None:
    journey = _journey([
        {"host": "h", "path": "/quote", "verbs": ["click"], "fields": [], "targets": ["Get quote"]},
    ])
    subj = subject_from_journey(journey, invariant_links=["inv-1"])
    assert evaluate(subj)["band"] == BAND_P0


def test_domain_boost_adds_signals_but_default_is_generic() -> None:
    generic = seed_pack()
    boosted = seed_pack("life_financial_insurance")
    assert len(boosted["signals"]) > len(generic["signals"])
    # A domain-only route (no generic marker) is banded only WITH the boost.
    subject = {"url_path": "/underwriting/step-1"}
    assert evaluate(subject)["band"] == BAND_P1  # generic seed → fail-up
    assert evaluate(subject, pack=boosted)["band"] == BAND_P0  # boosted → P0

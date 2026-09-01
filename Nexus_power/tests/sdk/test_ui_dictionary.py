"""Tests for the per-tenant UI dictionary signature + selector-merge logic.

Database-bound paths (lookup_signatures, record_recognitions,
record_automation_outcome) are exercised by the integration suite which
hits a live Postgres; here we validate the pure-Python pieces that
must remain stable: signature determinism, normalisation, and the
monotonic selector-replacement rule.
"""
from __future__ import annotations

import pytest

from nexus_sdk.dictionary import compute_element_signature
from nexus_sdk.dictionary.registry import (
    DictionaryHit,
    DictionaryRecognition,
    _normalize_element_type,
    _normalize_label,
    _normalize_page_key,
    _should_replace_selector,
)


# ─── Signature determinism ───────────────────────────────────────────────────

def test_same_inputs_produce_same_signature():
    s1 = compute_element_signature(
        page_key="usaa.insurance.life", element_type="button", label_text="Continue",
    )
    s2 = compute_element_signature(
        page_key="usaa.insurance.life", element_type="button", label_text="Continue",
    )
    assert s1 == s2
    # uuid5 hex is 32 characters
    assert len(s1) == 32


def test_signature_insensitive_to_label_whitespace_and_case():
    s1 = compute_element_signature(
        page_key="x", element_type="button", label_text="Submit",
    )
    s2 = compute_element_signature(
        page_key="x", element_type="button", label_text="  submit  ",
    )
    s3 = compute_element_signature(
        page_key="x", element_type="button", label_text="SUBMIT.",
    )
    assert s1 == s2 == s3


def test_signature_distinguishes_label_content():
    s1 = compute_element_signature(
        page_key="x", element_type="button", label_text="Save",
    )
    s2 = compute_element_signature(
        page_key="x", element_type="button", label_text="Cancel",
    )
    assert s1 != s2


def test_signature_distinguishes_element_type():
    s_button = compute_element_signature(
        page_key="x", element_type="button", label_text="Email",
    )
    s_input = compute_element_signature(
        page_key="x", element_type="text_field", label_text="Email",
    )
    assert s_button != s_input


def test_signature_distinguishes_page():
    s_login = compute_element_signature(
        page_key="usaa.login", element_type="button", label_text="Continue",
    )
    s_quote = compute_element_signature(
        page_key="usaa.insurance.life.quote", element_type="button", label_text="Continue",
    )
    assert s_login != s_quote


def test_signature_normalizes_element_type_dashes():
    """Hyphenated element types collapse to underscores so 'text-field'
    and 'text_field' produce the same signature."""
    s1 = compute_element_signature(
        page_key="x", element_type="text-field", label_text="Year",
    )
    s2 = compute_element_signature(
        page_key="x", element_type="text_field", label_text="Year",
    )
    assert s1 == s2


def test_signature_handles_empty_inputs():
    s1 = compute_element_signature(page_key="", element_type="", label_text="")
    s2 = compute_element_signature(page_key="", element_type="", label_text="")
    assert s1 == s2


def test_signature_distinguishes_zip_variants():
    """Two genuinely different fields must NOT collide even when the
    base label looks similar."""
    s_zip = compute_element_signature(
        page_key="x", element_type="text_field", label_text="ZIP",
    )
    s_zip4 = compute_element_signature(
        page_key="x", element_type="text_field", label_text="ZIP+4",
    )
    assert s_zip != s_zip4


# ─── Normalisation helpers ───────────────────────────────────────────────────

def test_normalize_label_strips_trailing_punctuation():
    assert _normalize_label("Submit Form.") == "submit form"
    assert _normalize_label("Click here!") == "click here"
    assert _normalize_label("quote") == "quote"


def test_normalize_element_type_collapses_dashes():
    assert _normalize_element_type("Text-Field") == "text_field"
    assert _normalize_element_type("Button") == "button"


def test_normalize_page_key_strips_trailing_dot():
    assert _normalize_page_key("usaa.insurance.") == "usaa.insurance"
    assert _normalize_page_key("USAA.LIFE") == "usaa.life"


def test_normalize_label_handles_none():
    assert _normalize_label(None) == ""  # type: ignore[arg-type]
    assert _normalize_element_type(None) == ""  # type: ignore[arg-type]


# ─── Selector-replacement rule ───────────────────────────────────────────────

def _hit(*, selector: str = "", confidence: float = 0.0, source: str = "unknown") -> DictionaryHit:
    return DictionaryHit(
        entry_id="e",
        signature="s",
        preferred_selector=selector,
        selector_confidence=confidence,
        selector_source=source,
        action_kind="",
        recognition_count=1,
        automation_success_count=0,
        automation_failure_count=0,
        bbox_centre_x=None,
        bbox_centre_y=None,
    )


def _rec(*, selector: str = "", confidence: float = 0.0, source: str = "unknown") -> DictionaryRecognition:
    return DictionaryRecognition(
        page_key="x",
        domain="example.com",
        element_type="button",
        label_text="Submit",
        preferred_selector=selector,
        selector_confidence=confidence,
        selector_source=source,
    )


def test_replace_when_no_prior():
    rec = _rec(selector="text=Submit", confidence=0.6, source="ocr")
    assert _should_replace_selector(None, rec) is True


def test_keep_prior_when_new_is_empty():
    """A prior selector must NEVER be replaced by an empty new selector
    — losing a working selector to OCR noise is exactly the regression
    the dictionary is meant to prevent."""
    prior = _hit(selector="text=Submit", confidence=0.8, source="ocr")
    rec = _rec(selector="", confidence=0.9, source="vision")
    assert _should_replace_selector(prior, rec) is False


def test_replace_when_new_confidence_higher():
    prior = _hit(selector="role=button[name=Save]", confidence=0.5, source="vision")
    rec = _rec(selector="text=Save", confidence=0.85, source="ocr")
    assert _should_replace_selector(prior, rec) is True


def test_keep_when_new_confidence_lower():
    prior = _hit(selector="text=Save", confidence=0.85, source="ocr")
    rec = _rec(selector="role=button", confidence=0.5, source="vision")
    assert _should_replace_selector(prior, rec) is False


def test_replace_on_tie_with_richer_source():
    """Same confidence + richer source (OCR-grounded > vision-only) wins."""
    prior = _hit(selector="role=button", confidence=0.7, source="vision")
    rec = _rec(selector="text=Save", confidence=0.7, source="ocr")
    assert _should_replace_selector(prior, rec) is True


def test_keep_on_tie_with_same_source():
    prior = _hit(selector="text=Save", confidence=0.7, source="ocr")
    rec = _rec(selector="text=Save Different", confidence=0.7, source="ocr")
    assert _should_replace_selector(prior, rec) is False


# ─── Recognition signature integration ───────────────────────────────────────

def test_recognition_signature_matches_compute_function():
    rec = DictionaryRecognition(
        page_key="usaa.insurance.life",
        domain="usaa.com",
        element_type="button",
        label_text="Continue",
    )
    direct = compute_element_signature(
        page_key="usaa.insurance.life",
        element_type="button",
        label_text="Continue",
    )
    assert rec.signature == direct

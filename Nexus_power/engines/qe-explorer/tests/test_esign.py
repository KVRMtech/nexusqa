"""P4 — eSignature widget recognition (deterministic, value-free)."""
from __future__ import annotations

from app.esign import (
    KIND_ATTEST,
    KIND_CANVAS_SIGNATURE,
    KIND_VENDOR_IFRAME,
    classify_esign_widget,
    is_esign_widget,
)


def test_canvas_signature_pad():
    assert classify_esign_widget({"name": "", "tag": "canvas"}) == KIND_CANVAS_SIGNATURE
    assert classify_esign_widget({"name": "Signature", "tag": "canvas"}) == KIND_CANVAS_SIGNATURE
    assert classify_esign_widget(
        {"name": "Draw your signature", "kind": "button"}) == KIND_CANVAS_SIGNATURE


def test_attest_controls():
    assert classify_esign_widget({"name": "I agree", "kind": "checkbox"}) == KIND_ATTEST
    assert classify_esign_widget({"name": "e-Sign", "kind": "button"}) == KIND_ATTEST
    assert classify_esign_widget(
        {"name": "Electronically Sign and Submit", "kind": "button"}) == KIND_ATTEST
    assert classify_esign_widget(
        {"name": "I consent to electronic signature", "kind": "checkbox"}) == KIND_ATTEST


def test_vendor_iframe():
    assert classify_esign_widget(
        {"kind": "iframe", "src": "https://demo.docusign.net/Signing/x"}) == KIND_VENDOR_IFRAME
    assert classify_esign_widget({"name": "Adobe Sign", "kind": "iframe"}) == KIND_VENDOR_IFRAME


def test_auth_is_never_an_esign_widget():
    # "Sign in/out/on" is authentication — the walk's auth guard, mirrored here.
    assert classify_esign_widget({"name": "Sign in", "kind": "button"}) is None
    assert classify_esign_widget({"name": "Sign out", "kind": "button"}) is None
    assert classify_esign_widget({"name": "Log in", "kind": "button"}) is None


def test_ordinary_controls_are_not_esign():
    assert classify_esign_widget({"name": "Continue", "kind": "button"}) is None
    assert classify_esign_widget({"name": "Email", "kind": "textbox"}) is None
    assert classify_esign_widget({}) is None
    assert classify_esign_widget("nope") is None


def test_is_esign_widget_wrapper():
    assert is_esign_widget({"name": "I agree", "kind": "checkbox"}) is True
    assert is_esign_widget({"name": "Continue"}) is False

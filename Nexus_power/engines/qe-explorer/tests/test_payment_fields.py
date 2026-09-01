"""P4 — the payment fill lane: card fields are filled with a SYNTHETIC card that
is format-valid (Luhn) and from a designated TEST bin — never a real PAN. This
locks that discipline so a regression can't slip a real-looking card in."""
from __future__ import annotations

from app import field_semantics as S
from app.field_values import value_for
from app.identity_pack import _TEST_BINS, derive


def _luhn_ok(number: str) -> bool:
    total, alt = 0, False
    for ch in reversed([c for c in number if c.isdigit()]):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def test_synthetic_card_is_luhn_valid_and_from_a_test_bin():
    ident = derive("seed-payment-1")
    assert ident.card_number.isdigit()
    assert _luhn_ok(ident.card_number)                 # format a gateway accepts
    assert any(ident.card_number.startswith(b) for b in _TEST_BINS)   # a TEST bin


def test_card_number_is_deterministic_and_seed_scoped():
    assert derive("seed-x").card_number == derive("seed-x").card_number
    assert derive("seed-x").card_number != derive("seed-y").card_number


def test_value_for_fills_card_fields_from_the_synthetic_identity():
    ident = derive("seed-payment-2")
    assert value_for(S.CARD_NUMBER, {}, ident) == ident.card_number
    assert value_for(S.CARD_CVC, {}, ident) == ident.card_cvc
    assert value_for(S.CARD_EXPIRY, {}, ident) == ident.card_expiry

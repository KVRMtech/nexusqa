"""PRODUCT AND PAYMENT CADENCE COME FROM THE HOUSEHOLD, ON EVERY PAGE.

THE INCOHERENCE THIS PREVENTS.  A funnel asks about its product more than
once — a card grid at the quote's start, a "Product" select on the
application, a "Plan type" on review — and until now every ask past the first
was answered by "no persona attribute matches this enumeration, so the first
option was taken".  First-option is a different answer on differently-ordered
lists, and a quote taken on one product with an application submitted on
another is exactly the cross-step contradiction an underwriting rule checks.
Same story for premium cadence: the persona's money is derived monthly-first,
and a "Premium Mode" answered by list order can contradict the monthly premium
figure typed two fields up.

So both are PERSONA attributes now, resolved through the same
``_choice_targets`` rung as gender and tobacco — one household, one answer,
wherever the question appears.
"""
from __future__ import annotations

from app.fill_engine.generator import generate
from app.fill_engine.persona import derive_persona

_PERSONA = derive_persona("qec-char::product")

_SUMMIT_PRODUCTS = ["Term Life - 10 Year", "Term Life - 20 Year",
                    "Term Life - 30 Year", "Whole Life", "Universal Life",
                    "Variable Universal Life", "Indexed Universal Life"]
_MODES = ["Monthly", "Quarterly", "Semi-Annual", "Annual"]


def _select(name, options):
    return {"role": "combobox", "kind": "select", "name": name, "tag": "select",
            "options": list(options)}


# ── product ────────────────────────────────────────────────────────────────

def test_a_product_select_answers_with_the_households_term_product():
    cand = generate("", _select("Product", _SUMMIT_PRODUCTS), _PERSONA,
                    kind="select", name="Product")
    assert cand.value is not None
    assert "term life" in cand.value.lower(), (
        "the household's product is a term life policy; got %r" % cand.value)
    assert cand.source == "term_years", (
        "the answer must be traceable to the persona, not to list order")


def test_two_pages_spelling_the_list_differently_get_the_same_product():
    """THE COHERENCE HALF: the quote page and the application step offer
    different labels in different orders, and the household's answer must be
    the same product on both."""
    a = generate("", _select("Product Type",
                             ["Whole Life", "Term Life", "Universal Life"]),
                 _PERSONA, kind="select", name="Product Type")
    b = generate("", _select("Product", _SUMMIT_PRODUCTS), _PERSONA,
                 kind="select", name="Product")
    assert a.value is not None and b.value is not None
    assert "term" in a.value.lower() and "term" in b.value.lower(), (
        "one household, one product: %r vs %r" % (a.value, b.value))


def test_control_a_list_with_no_term_product_is_not_forced():
    """The persona PREFERS; it never fabricates.  A list that offers no term
    product falls through to the rungs that always existed."""
    cand = generate("", _select("Product", ["Whole Life", "Universal Life"]),
                    _PERSONA, kind="select", name="Product")
    assert cand.value in ("Whole Life", "Universal Life"), (
        "the fallback must still answer from the application's own list")
    assert cand.source != "term_years"


# ── premium cadence ────────────────────────────────────────────────────────

def test_premium_mode_agrees_with_the_monthly_money():
    cand = generate("", _select("Premium Mode", _MODES), _PERSONA,
                    kind="select", name="Premium Mode")
    assert cand.value == "Monthly"
    assert cand.source == "money.monthly_premium"


def test_payment_frequency_and_billing_cycle_are_the_same_question():
    for label in ("Payment Frequency", "Billing Cycle"):
        cand = generate("", _select(label, _MODES), _PERSONA,
                        kind="select", name=label)
        assert cand.value == "Monthly", "%r missed the cadence rung" % label


def test_control_premium_alone_is_still_money_not_cadence():
    """Each token set alone is too generic, and the AND is asserted from both
    sides: a bare "Premium" field is an AMOUNT (money's rung), and a bare
    "Mode" select is nobody's cadence."""
    amount = generate("currency_amount", {"role": "textbox", "kind": "text",
                                   "name": "Annual Premium", "tag": "input",
                                   "options": []},
                      _PERSONA, kind="text", name="Annual Premium")
    assert amount.value is not None
    assert amount.source == "money.annual_premium", (
        "the money rung must keep owning bare premium amounts; got %r"
        % amount.source)
    mode_only = generate("", _select("Mode", _MODES), _PERSONA,
                         kind="select", name="Mode")
    assert mode_only.source != "money.monthly_premium", (
        "a bare 'Mode' select must not be read as a premium cadence")


def test_control_payment_method_is_not_a_cadence_question():
    """"Payment Method" carries the cadence set's first token and none of the
    second — Card / ACH / Check is a different fork and must fall through."""
    cand = generate("", _select("Payment Method", ["Card", "ACH", "Check"]),
                    _PERSONA, kind="select", name="Payment Method")
    assert cand.value in ("Card", "ACH", "Check")
    assert cand.source != "money.monthly_premium"

"""Unit tests for :mod:`app.fingerprint` — SPA-state-aware page identity."""
from __future__ import annotations

from app.fingerprint import (
    interactive_signature,
    state_fingerprint,
    url_template,
)


def _ctrl(role, name, disabled=False, kind=None):
    c = {"role": role, "name": name, "disabled": disabled}
    if kind:
        c["kind"] = kind
    return c


LOGIN_FORM = [
    _ctrl("textbox", "Username"),
    _ctrl("textbox", "Password"),
    _ctrl("button", "Sign in"),
]


# ─── url_template ──────────────────────────────────────────────────────────────


def test_url_template_collapses_numeric_ids():
    assert url_template("https://app.acme.example/orders/123") == "app.acme.example/orders/*"
    assert url_template("https://app.acme.example/orders/456") == "app.acme.example/orders/*"
    assert (url_template("https://app.acme.example/orders/123")
            == url_template("https://app.acme.example/orders/456"))


def test_url_template_collapses_uuid_and_hex_and_mixed_ids():
    uuid = "https://x.example/users/f47ac10b-58cc-4372-a567-0e02b2c3d479/profile"
    assert url_template(uuid) == "x.example/users/*/profile"
    assert url_template("https://x.example/blob/deadbeefcafe0001") == "x.example/blob/*"
    # A whole id-shaped segment collapses (so /order-00042 and /order-00043 share
    # a template) — not a partial replacement inside the segment.
    assert url_template("https://x.example/order-00042/items") == "x.example/*/items"
    assert (url_template("https://x.example/order-00042/items")
            == url_template("https://x.example/order-00043/items"))


def test_url_template_keeps_route_names():
    assert url_template("https://x.example/orders/new") == "x.example/orders/new"
    assert url_template("https://x.example/v2/dashboard") == "x.example/v2/dashboard"


def test_url_template_drops_query_and_scheme_and_port():
    a = url_template("https://x.example:8443/search?utm_source=ad&session=abc")
    b = url_template("http://x.example/search")
    assert a == b == "x.example/search"


def test_url_template_preserves_pagination_so_pages_are_distinct_states():
    """R1: dropping the whole query collapsed every page of a listing into one
    state, so the crawler never advanced past page 1. Pagination params are now
    preserved (only those params — cosmetic query stays dropped)."""
    p1 = url_template("https://x.example/products?page=1&utm_source=ad")
    p2 = url_template("https://x.example/products?page=2&utm_source=ad")
    p3 = url_template("https://x.example/products?page=3")
    assert p1 != p2 != p3, "paginated views must be DISTINCT states"
    assert p2 == "x.example/products?page=2", p2      # only the pagination param kept
    # cosmetic-only query still collapses to the bare path
    assert url_template("https://x.example/products?utm_source=ad&ref=x") == "x.example/products"
    # offset/start style pagination also recognised; keys normalised + sorted
    assert (url_template("https://x.example/list?OFFSET=20")
            == "x.example/list?offset=20")
    assert (url_template("https://x.example/list?start=40&page=3")
            == "x.example/list?page=3&start=40")


def test_url_template_normalises_spa_hash_route_and_drops_scroll_anchor():
    assert url_template("https://x.example/#/orders/99") == "x.example/#/orders/*"
    assert url_template("https://x.example/app#!/dashboard") == "x.example/app#!/dashboard"
    # a bare scroll anchor is cosmetic → dropped
    assert url_template("https://x.example/page#section-2") == "x.example/page"


# ─── interactive_signature ─────────────────────────────────────────────────────


def test_signature_is_sorted_deduped_and_excludes_static():
    controls = [
        _ctrl("button", "Sign in"),
        {"role": "heading", "name": "Welcome back, Jane"},  # static → excluded
        _ctrl("textbox", "Username"),
        _ctrl("button", "Sign in"),                          # duplicate → deduped
    ]
    sig = interactive_signature(controls)
    assert sig == [["button", "sign in", "0"], ["textbox", "username", "0"]]


def test_signature_includes_records_with_control_kind():
    # A record from build_inventory carries a refined kind even if role is odd.
    sig = interactive_signature([{"role": "presentation", "name": "Amount", "kind": "text"}])
    assert sig == [["presentation", "amount", "0"]]


# ─── state_fingerprint invariants ──────────────────────────────────────────────


def test_fingerprint_is_stable_across_runs_and_control_order():
    a = state_fingerprint("https://x.example/login", LOGIN_FORM)
    b = state_fingerprint("https://x.example/login", list(reversed(LOGIN_FORM)))
    assert a == b
    assert len(a) == 64 and int(a, 16) >= 0   # valid sha256 hex


def test_cosmetic_text_change_does_not_move_fingerprint():
    with_greeting = LOGIN_FORM + [{"role": "heading", "name": "Hello Jane!"}]
    with_other = LOGIN_FORM + [{"role": "heading", "name": "Welcome, Priya"}]
    assert (state_fingerprint("https://x.example/login", with_greeting)
            == state_fingerprint("https://x.example/login", with_other)
            == state_fingerprint("https://x.example/login", LOGIN_FORM))


def test_same_url_template_same_state_same_fingerprint():
    order_grid = [_ctrl("button", "View"), _ctrl("button", "Reorder")]
    assert (state_fingerprint("https://x.example/orders/123", order_grid)
            == state_fingerprint("https://x.example/orders/456", order_grid))


def test_new_required_field_moves_fingerprint():
    base = state_fingerprint("https://x.example/apply", LOGIN_FORM)
    grown = state_fingerprint(
        "https://x.example/apply",
        LOGIN_FORM + [_ctrl("textbox", "SSN")],
    )
    assert base != grown


def test_disabled_state_change_moves_fingerprint():
    enabled = [_ctrl("button", "Submit", disabled=False)]
    disabled = [_ctrl("button", "Submit", disabled=True)]
    assert (state_fingerprint("https://x.example/f", enabled)
            != state_fingerprint("https://x.example/f", disabled))


def test_query_params_do_not_move_fingerprint():
    assert (state_fingerprint("https://x.example/s?q=shoes&utm=ad", LOGIN_FORM)
            == state_fingerprint("https://x.example/s", LOGIN_FORM))


def test_dialog_flags_move_fingerprint():
    closed = state_fingerprint("https://x.example/policy", LOGIN_FORM)
    open_modal = state_fingerprint("https://x.example/policy", LOGIN_FORM,
                                   dialog_flags=["modal:confirm-delete"])
    assert closed != open_modal
    # flag normalisation is order/dup/case-insensitive → stable
    assert (state_fingerprint("https://x.example/policy", LOGIN_FORM,
                              dialog_flags=["A", "b", "a"])
            == state_fingerprint("https://x.example/policy", LOGIN_FORM,
                                 dialog_flags=["b", "A"]))


def test_different_route_different_fingerprint():
    assert (state_fingerprint("https://x.example/login", LOGIN_FORM)
            != state_fingerprint("https://x.example/dashboard", LOGIN_FORM))

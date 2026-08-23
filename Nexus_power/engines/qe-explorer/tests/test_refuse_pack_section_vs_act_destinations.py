"""R7' — a SECTION named after a verb is not the act. Pinned three ways.

WHAT R7' CHANGED
================
``rp.verb.pay`` and ``rp.verb.underwrite`` each carried one broad regex used for
BOTH the control's label and its destination. That is right for a label and
wrong for a path, because "payment"/"payments"/"underwriting" are the names of
SECTIONS of an insurance console:

    link -> /underwriting/new-business    matched \\bunderwriting\\b   -> critical
    link -> /policy-admin/payments        matched \\bpayments\\b       -> critical

Every link into those sections was refused as an irreversible commit, which is
why the summit-life-carrier crawl visited 8 routes and never reached the wizard
holding its own commit control. Entering a section commits nothing.

So each rule was split: the broad vocabulary keeps ``button_name``, and a narrow
segment-anchored pattern takes ``url_path``:

    rp.verb.pay_path         (^|/)(pay|payout|payouts|repay|prepay|autopay|remit|remittance)(/|$)
    rp.verb.underwrite_path  (^|/)(underwrite)(/|$)

This is the shape ``rp.get.destructive_path`` already uses — enumerate the ACT
segments, anchor them to whole path segments, leave the section nouns out — and
the reasoning ``rp.verb.admin`` already carries in this pack: *"an /admin PATH is
a fence concern, not an irreversible verb"*.

WHY THIS MODULE IS AT THE INVENTORY LAYER
=========================================
``tests/test_guard.py`` pins the same split at ``classify_action_verb``. This
module pins it through :func:`build_inventory`, which is **the layer that
actually gates the crawl**: a control marked ``danger`` is dropped from every
advance tier, so this is where an over-block stops a funnel and where an
under-block would let one through.

That distinction is not academic. An earlier attempt at this fix was justified by
calling ``classify_control_danger`` directly with the PAGE url — something
``build_inventory`` never does, because it passes each control's own
DESTINATION. The measurement looked like a page-wide over-block that did not
exist, and the proposed fix would have removed destination refusal entirely.
Testing at the layer the crawler really uses is what makes this file evidence.

THE THREE DIRECTIONS
====================
1. section destinations           -> allowed (the bug R7' fixes);
2. destructive destinations       -> still refused (R7' must not have reached them);
3. act-shaped money/uw dests      -> still refused (the narrow rules must still bite);
   plus labels                    -> still refused (the broad regex still binds to names).
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.guard import load_refuse_pack
from app.inventory import build_inventory

_PACK = load_refuse_pack(Settings().refuse_pack_path)

#: A page with no verb in its own path, so nothing here can be a page artefact.
_PAGE = "https://admin.example/dashboard/overview"


def _raw(role: str, name: str, **over):
    base = {
        "role": role, "name": name, "name_source": "content", "best_effort": False,
        "kind": role, "tag": over.pop("tag", "button"), "input_type": "",
        "options": [], "required": False, "disabled": False,
        "frame_selector": "", "testid": "", "css_hint": "", "value_committed": "",
        "landmark": {"role": "", "name": ""},
    }
    base.update(over)
    return base


def _link_danger(href: str) -> bool:
    ctrl = _raw("link", "Go", tag="a", href=href)
    return bool(build_inventory([ctrl], _PACK, url=_PAGE)[0]["danger"])


def _button_danger(label: str) -> bool:
    return bool(build_inventory([_raw("button", label)], _PACK, url=_PAGE)[0]["danger"])


# ── 1 · a section named after a verb is navigation ──────────────────────────

@pytest.mark.parametrize("href", [
    "/underwriting/new-business",
    "/underwriting/new-business/new-application",
    "/underwriting/queue",
    "/policy-admin/payments",
    "/billing/payments",
    "/payments/history",
])
def test_a_link_into_a_verb_named_SECTION_is_not_dangerous(href):
    assert _link_danger(href) is False, (
        f"{href} is refused. It is a section, not an act — this is the over-block "
        f"that sealed the summit-life-carrier funnel."
    )


# ── 2 · R7' must not have reached the destructive rules ─────────────────────

@pytest.mark.parametrize("href", [
    "/account/delete", "/records/destroy", "/admin/users/purge",
    "/things/1/remove", "/data/erase",
])
def test_a_destructive_destination_is_still_refused(href):
    assert _link_danger(href) is True, (
        f"{href} is allowed. R7' touched only rp.verb.pay and rp.verb.underwrite; "
        f"if a destructive path stopped being refused, it reached further than it "
        f"was authorised to."
    )


# ── 3 · the narrow rules must still bite ────────────────────────────────────

@pytest.mark.parametrize("href", [
    "/pay", "/api/pay", "/invoices/42/pay",
    "/payout", "/payouts", "/remit", "/repay", "/autopay",
    "/underwrite", "/applications/7/underwrite",
])
def test_a_destination_that_IS_the_act_is_still_refused(href):
    assert _link_danger(href) is True, (
        f"{href} is allowed. The whole premise of R7' is that the ACT segments "
        f"keep matching while the section nouns stop — if this fails, the "
        f"narrowing went too far and money destinations are unguarded."
    )


@pytest.mark.parametrize("label", [
    "Pay Now", "Make Payment", "Process Payment", "Payout",
    "Submit to Underwriting", "Underwrite Now",
    "Bind Policy", "Delete Account", "Transfer Funds",
])
def test_a_label_that_names_the_act_is_still_refused(label):
    """The broad vocabulary still binds to the NAME — that half was never the
    problem, and R7' must not have cost it anything."""
    assert _button_danger(label) is True


# ── the fence ───────────────────────────────────────────────────────────────

def test_the_path_rules_stay_segment_anchored():
    """A path rule that loses its anchors becomes the blanket again.

    ``(^|/)…(/|$)`` is what keeps `/pay` matching while `/policy-admin/payments`
    does not. Without this, someone relaxing the pattern to a bare `\\bpay\\b`
    reintroduces the exact defect R7' removed, and every test above still passes
    except the section ones — which is a long way to find out.
    """
    by_id = {r.id: r for r in _PACK.irreversible_verbs}
    for rule_id in ("rp.verb.pay_path", "rp.verb.underwrite_path"):
        assert rule_id in by_id, f"{rule_id} is gone — the R7' split was undone"
        rule = by_id[rule_id]
        assert rule.match.startswith("(^|/)"), f"{rule_id} lost its leading anchor"
        assert rule.match.endswith("(/|$)"), f"{rule_id} lost its trailing anchor"
        assert tuple(rule.applies_to) == ("url_path",), (
            f"{rule_id} must bind to url_path ONLY: it is the destination half of "
            f"a split whose label half lives on the sibling rule."
        )


def test_the_label_rules_no_longer_match_urls():
    """The other half of the split, asserted on the pack rather than inferred."""
    by_id = {r.id: r for r in _PACK.irreversible_verbs}
    for rule_id in ("rp.verb.pay", "rp.verb.underwrite"):
        assert tuple(by_id[rule_id].applies_to) == ("button_name",), (
            f"{rule_id} matches a URL again — its vocabulary contains section "
            f"nouns, so this is the over-block returning."
        )

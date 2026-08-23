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
segment-anchored pattern takes the DESTINATION (``url_path`` + ``url_query``).
The narrow patterns match an act segment with an optional ``-``/``.`` suffix
(``/pay-now``, ``/pay.php``), a payment noun followed by an act verb
(``/payments/42/execute``), the phrase ``submit-to-underwriting``, and the verb
carried in a query parameter in either position (``?action=pay``, ``?pay=1``).
Every one of those forms was added because a non-author red-team found the first
cut had stopped refusing it — see the pinned cases at the end of this module.

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

def test_the_path_rules_still_separate_a_section_from_an_act():
    """THE FENCE, stated behaviourally rather than as regex text.

    An earlier version of this asserted the patterns literally started ``(^|/)``
    and ended ``(/|$)``. That broke the moment the patterns grew alternations to
    close the red-team's findings — and it was the wrong invariant anyway: what
    must hold is not the shape of the regex but that a SECTION does not match and
    an ACT does. Asserted directly, so the rules may be rewritten freely as long
    as the property survives.
    """
    by_id = {r.id: r for r in _PACK.irreversible_verbs}
    for rule_id in ("rp.verb.pay_path", "rp.verb.underwrite_path"):
        assert rule_id in by_id, f"{rule_id} is gone — the R7' split was undone"
        assert "button_name" not in by_id[rule_id].applies_to, (
            f"{rule_id} is the DESTINATION half of a split; its label half lives "
            f"on the sibling rule and the broad vocabulary must not reach names."
        )
    # a section is not an act …
    for href in ("/payments/history", "/underwriting/queue", "/policy-admin/payments"):
        assert _link_danger(href) is False, f"{href} matched — the section leak is back"
    # … and an act is still an act
    for href in ("/pay", "/underwrite", "/payout"):
        assert _link_danger(href) is True, f"{href} stopped matching — the rule went blind"


# ── the non-author red-team's findings, pinned so they cannot recur ─────────
# Reported by session `nexusqa-2d` against d3ed533 from a fresh clone, measured
# through build_inventory. Thirteen destinations were refused by pack v1 and
# allowed by the first cut of v2 — a real hole, opened by this fix. Two causes:
#
#   (a) the segment anchor `(/|$)` rejected the hyphen/dot suffixes that v1's
#       `` accepted, so `/pay-now` — the act, by the rule's own description —
#       stopped matching;
#   (b) `url_query` silently left both rules when their `applies_to` narrowed,
#       and the assumed route-layer backstop did not exist: rp.get.action_mutation
#       listed neither `underwrite` nor `remit`.
#
# Every case below is one the red-team found, not one this author imagined.

@pytest.mark.parametrize("href", [
    "/pay-now", "/pay.php", "/remit-now", "/autopay-enroll",      # (a) suffixes
    "/submit-to-underwriting",                                     # (b) lost alternative
    "/new-business/submit-to-underwriting",
    "/payments/42/execute", "/payment/execute",                    # section + act verb
])
def test_redteam_act_shaped_paths_are_refused(href):
    assert _link_danger(href) is True, (
        f"{href} is allowed. Pack v1 refused it; if v2 does not, this fix opened "
        f"a hole rather than closing one."
    )


@pytest.mark.parametrize("href", [
    "/x?action=underwrite", "/x?step=underwrite", "/x?do=remit",
    "/x?pay=1", "/x?action=pay", "/x?underwrite=1",
])
def test_redteam_query_borne_acts_are_refused(href):
    """Both positions: the verb as a param VALUE, and as a param KEY."""
    assert _link_danger(href) is True, (
        f"{href} is allowed. url_query coverage was lost once already when these "
        f"rules narrowed — this is the assertion that notices."
    )


def test_the_route_layer_backstop_actually_lists_these_verbs():
    """The assumption that cost the hole, now checked instead of assumed.

    The first cut of R7' justified dropping url_query on the grounds that
    rp.get.action_mutation would still catch query-borne verbs at the request
    layer. It would not have: its verb list contained neither `underwrite` nor
    `remit`. Asserting the backstop's CONTENTS is the difference between a
    backstop and a belief in one.
    """
    rule = next(r for r in _PACK.mutation_signal_get_rules
                if r.id == "rp.get.action_mutation")
    for verb in ("underwrite", "remit", "pay", "transfer", "disburse"):
        assert verb in rule.match, (
            f"rp.get.action_mutation does not list {verb!r}, so a query-borne "
            f"{verb} has no request-layer backstop."
        )


def test_the_label_rules_no_longer_match_urls():
    """The other half of the split, asserted on the pack rather than inferred."""
    by_id = {r.id: r for r in _PACK.irreversible_verbs}
    for rule_id in ("rp.verb.pay", "rp.verb.underwrite"):
        assert tuple(by_id[rule_id].applies_to) == ("button_name",), (
            f"{rule_id} matches a URL again — its vocabulary contains section "
            f"nouns, so this is the over-block returning."
        )


# ── FINDING B, from the red-team's RE-verification of the fix for findings 1-3 ──
# The first repair of finding 1 widened the act segments with `[-._][a-z0-9-]*`
# — verb followed by ANYTHING. That closed `/pay-now` and simultaneously
# reopened the R7 class in miniature: a read-only section whose name is a verb
# plus a hyphenated qualifier was refused as an act. Fail-closed, so a coverage
# loss rather than a safety hole, but `/remittance-advice` and `/payout-history`
# are ordinary pages in a carrier billing console.
#
# The fix is the same discipline as everywhere else in this pack: ENUMERATE. An
# act qualifier (`-now`, `-confirm`, `-submit`, `-execute`, `-process`,
# `-enroll`) or an endpoint extension (`.php`, `.aspx`, …) is an act; an
# arbitrary noun is a section. A regex cannot tell "pay-NOW" from
# "payout-HISTORY" structurally, so the vocabulary is listed rather than guessed.

@pytest.mark.parametrize("href", [
    "/remittance-advice", "/remittance-report", "/remittance-detail/42",
    "/remit-advice", "/remit-summary",
    "/payout-history", "/repay-plan", "/prepay-calculator", "/autopay-settings",
    "/underwrite-checklist",
])
def test_a_verb_named_section_with_a_qualifier_is_still_navigation(href):
    """Finding B: these read; they do not act."""
    assert _link_danger(href) is False, (
        f"{href} is refused. A verb followed by a NOUN is a section — this is the "
        f"R7 over-block class returning through the suffix alternation."
    )


@pytest.mark.parametrize("href", [
    "/pay-now", "/pay.php", "/remit-now", "/autopay-enroll",
    "/pay-confirm", "/underwrite-submit",
])
def test_a_verb_with_an_ACT_qualifier_is_still_refused(href):
    """The other side of Finding B's fix: narrowing the suffix must not undo the
    original finding-1 repair."""
    assert _link_danger(href) is True, (
        f"{href} is allowed. `-now`/`.php`/`-enroll` mark the act, and losing "
        f"them re-opens the under-block the red-team found first."
    )

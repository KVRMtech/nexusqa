"""LINKS THAT STAY IN THE OPERATOR'S AREA OUTRANK SITE CHROME.

MEASURED (Dolibarr, 2026-08-29). The crawl was pointed at the proposals list and
spent all 19 of its states here:

    /adherents/  /societe/  /product/  /mrp/  /projet/  /compta/bank/
    /accountancy/  /hrm/  /ecm/  /ticket/  /core/tools  /website/  /admin/ ...

Not one proposal was opened. `card.php` appears ZERO times in the whole bundle.

WHY, EXACTLY. The list page carries 26 links to `/comm/propal/card.php?id=N`
and 25 top-nav links to other modules. All are plain in-scope hrefs and all were
enqueued. The frontier orders on `(priority, novelty_rank, depth, seq)`; nothing
sets priority, the id-routes collapse to ONE reach key so the cards contribute a
single item, and every nav link is its own section -- so 26 items tie and the
tiebreak falls through to `seq`, which is DOM order. The nav menu is above the
table. Site chrome won the budget.

THE RULE. A destination that stays under the path the operator onboarded is the
work; one that leaves it is chrome. `/comm/propal/list.php` ->
`/comm/propal/card.php` stays; `/adherents/index.php` does not. Structural, and
language-free: no vocabulary decides what a menu is called.

Ordering only. Nothing becomes unreachable -- chrome is still crawled, after the
thing we were asked to crawl.
"""
from __future__ import annotations

from app.discovery import area_priority

ENTRY = "http://app/comm/propal/list.php"


def test_a_record_under_the_entry_path_comes_first():
    assert area_priority("http://app/comm/propal/card.php?id=36", ENTRY) < 0


def test_a_different_module_comes_after():
    assert area_priority("http://app/adherents/index.php", ENTRY) > 0


def test_the_ranking_puts_the_record_ahead_of_the_menu():
    """THE MEASURED CASE, as an ordering."""
    card = area_priority("http://app/comm/propal/card.php?id=36", ENTRY)
    nav = area_priority("http://app/societe/index.php?mainmenu=companies", ENTRY)
    assert card < nav


# ── controls ───────────────────────────────────────────────────────────────

def test_a_sibling_page_in_the_same_area_also_comes_first():
    assert area_priority("http://app/comm/propal/list.php?page=2", ENTRY) < 0


def test_an_offsite_url_is_not_promoted():
    assert area_priority("http://other/comm/propal/card.php", ENTRY) > 0


def test_a_shallow_entry_promotes_nothing_wrongly():
    """Pointed at the site root, every path is 'in area' -- so nothing is
    promoted and ordering is left exactly as it was."""
    assert area_priority("http://app/anything.php", "http://app/") == 0


def test_a_missing_url_is_neutral():
    assert area_priority("", ENTRY) == 0
    assert area_priority("http://app/x", "") == 0

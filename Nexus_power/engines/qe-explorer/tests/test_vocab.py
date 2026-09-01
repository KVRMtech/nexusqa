"""Language-pack vocabulary — union compilation and the Tier-2 destination
shape rule.

Laws under test:
  * the ``en`` pack compiles byte-identically to the historical inline
    patterns (every existing test and the qe-central parity pin still hold);
  * a commit word is tolerated in an advance label ONLY as a destination —
    conjunction shapes end at the boundary instead of being clicked;
  * adding a pack can only WIDEN the commit veto (fail-closed by
    construction).
"""
from __future__ import annotations

from app import vocab
from app.crawler import _WIZARD_ADVANCE_RE, _WIZARD_COMMIT_RE


def test_en_pack_compiles_to_the_historical_advance_pattern():
    assert vocab.ADVANCE_RE.pattern == r"\b(next|continue|proceed|forward)\b"
    assert _WIZARD_ADVANCE_RE is vocab.ADVANCE_RE


def test_en_pack_compiles_to_the_historical_commit_pattern():
    assert vocab.COMMIT_RE.pattern == (
        r"\b(submit|send|pay|paying|paid|payment|payments|buy|buying|purchase|"
        r"purchasing|order|checkout|check\s*out|place\s*order|confirm|finish|"
        r"complete|done|agree|accept|sign|book|reserve|schedule|activate|create|"
        r"register|subscribe|delete|cancel|remove|apply)\b"
    )
    assert _WIZARD_COMMIT_RE is vocab.COMMIT_RE


def test_destination_shape_permits_navigation_labels():
    assert vocab.is_destination_advance("Continue to Payment")
    assert vocab.is_destination_advance("Proceed to Checkout")
    assert vocab.is_destination_advance("Continue to Signature")
    assert vocab.is_destination_advance("Continue")          # no commit word
    assert vocab.is_destination_advance("Next")


def test_destination_shape_rejects_conjunction_and_prefix_commits():
    # Conjunction: the label SAYS it commits — never a Tier-2 advance.
    assert not vocab.is_destination_advance("Continue & Place Order")
    assert not vocab.is_destination_advance("Continue and Pay")
    # No destination preposition between advance and commit word.
    assert not vocab.is_destination_advance("Next: Confirm Order")
    # Commit word BEFORE the preposition.
    assert not vocab.is_destination_advance("Pay to Continue")
    # No advance word at all.
    assert not vocab.is_destination_advance("See My Quote")
    assert not vocab.is_destination_advance("")


def test_union_is_order_preserving_and_deduplicated():
    packs = dict(vocab.LANGUAGE_PACKS)
    try:
        vocab.LANGUAGE_PACKS["zz"] = {
            "advance": ["continue", "weiter"],
            "commit": ["submit", "kaufen"],
            "destination_prepositions": ["to", "zur"],
        }
        advance = vocab.compile_advance_re()
        commit = vocab.compile_commit_re()
        # 'en' order first and unchanged; new words appended once.
        assert advance.pattern.startswith(r"\b(next|continue|proceed|forward|weiter")
        assert advance.pattern.count("continue") == 1
        assert commit.pattern.endswith(r"|kaufen)\b")
        # WIDER only: everything the en pattern vetoed is still vetoed.
        assert commit.search("Submit Application")
        assert commit.search("Jetzt kaufen")
    finally:
        vocab.LANGUAGE_PACKS.clear()
        vocab.LANGUAGE_PACKS.update(packs)


def test_a_new_pack_never_narrows_the_commit_veto():
    """Every alternative in every pack survives into the union — fail-closed
    by construction."""
    union = vocab.compile_commit_re().pattern
    for pack in vocab.LANGUAGE_PACKS.values():
        for word in pack["commit"]:
            assert word in union

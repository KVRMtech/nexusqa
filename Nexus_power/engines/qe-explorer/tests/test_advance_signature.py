"""M2.6 / T-CAP-02 — a DETERMINISTIC advance is remembered too.

The bug: ``signature`` — the key tenant advance memory is stored under — was
produced only by the tier-3 agent oracle, and qe-central's harvest additionally
filtered on ``advance.oracle``. Two independent gates, same effect: on an
application whose forward controls are named "Next" or "Continue" the crawl
proved an advance at every step of every wizard and remembered NONE of them. The
learning layer only ever saw the rare case it was cheapest to learn.

The fix has to satisfy a contract the explorer cannot verify by itself: the key
a deterministic advance is STORED under must be the key qe-central computes when
it later RECALLS. The two services share no library, so the hash is mirrored
(:mod:`app.advance_signature`) and pinned on both sides against a frozen vector
— the same doctrine the commit vocabulary already lives under.

Mirrored pin: qe-central/tests/test_advance_memory.py::test_signature_parity_vector
"""
from __future__ import annotations

import asyncio

from app.advance_signature import compute_signature
from app.config import Settings
from app.crawler import (_WIZARD_COMMIT_RE, Budget, Crawler,
                         GuardContext, ORACLE_PICKED)
from app.guard import load_refuse_pack

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)


class _DummyPort:
    """The pick methods never touch the port."""


def _build(tmp_path, *, oracle=None, approvals=()):
    return Crawler(
        _DummyPort(),
        crawl_id="c1", tenant_id="t1", target_url="https://app.example/form",
        work_dir=str(tmp_path), refuse_pack=_REFUSE_PACK, budget=Budget(rate_per_s=0),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE_PACK, attestation=None),
        crawl_mode="e2e", submit_approvals=approvals, advance_oracle=oracle,
    )


def _btn(name, **kw):
    return {"name": name, "kind": "button", **kw}


def _pick(c, controls, title="Step"):
    return asyncio.run(
        c._pick_advance_e2e(controls, "https://app.example/x", title, "fp1"))


# ── The cross-service contract, frozen as data ──────────────────────────

#: One decision point, spelled out. Both services hash it to the value below.
PARITY_CONTROLS = [
    {"kind": "button", "name": "Continue"},
    {"kind": "link", "name": "Back to Quote"},
    {"kind": "button", "name": "  SAVE   Draft "},
]
PARITY_TITLE = "Step 2 of 4 - Coverage 12345"
PARITY_SIGNATURE = (
    "1063a6f6feeaa9bdae95e55ce8a573ee11af034fcc959b3f4a007c62c9cd00c9")


def test_signature_parity_vector():
    """A cross-process contract cannot be proven inside one process, so it is
    frozen as DATA that both processes assert against. If this fails, the
    explorer and qe-central no longer agree on where a memory lives — every
    remembered advance silently stops being recalled."""
    assert compute_signature(PARITY_CONTROLS, PARITY_TITLE) == PARITY_SIGNATURE


def test_signature_is_order_free_and_whitespace_and_case_normalised():
    """The same decision point reached twice must be the same decision point."""
    shuffled = [PARITY_CONTROLS[2], PARITY_CONTROLS[0], PARITY_CONTROLS[1]]
    assert compute_signature(shuffled, PARITY_TITLE) == PARITY_SIGNATURE
    assert compute_signature(
        [{"kind": "button", "name": "CONTINUE"}], "Q") == compute_signature(
        [{"kind": "button", "name": " continue "}], "Q")


def test_signature_ignores_digits_in_the_title():
    """A title carrying an order number must not give every visit its own
    decision point — the memory would never hit twice."""
    assert (compute_signature(PARITY_CONTROLS, "Quote 8891 - Review")
            == compute_signature(PARITY_CONTROLS, "Quote 4412 - Review"))


def test_signature_changes_when_the_offered_controls_change():
    assert (compute_signature(PARITY_CONTROLS, PARITY_TITLE)
            != compute_signature(PARITY_CONTROLS[:2], PARITY_TITLE))


def test_signature_carries_no_readable_text():
    """It is persisted as tenant memory and echoed into crawl evidence, so it
    is a one-way digest and nothing else — no label, no title, no URL, no host
    survives into it in readable form.

    Note the honest limit of that claim: the title's WORD shape IS part of the
    basis, so two titles differing by a word are two different decision points.
    That is the pre-existing qe-central basis this mirrors, and it is why only
    the DIGITS are dropped (the test above) — dropping words too would fuse
    genuinely different steps of a wizard into one memory."""
    sig = compute_signature(
        [{"kind": "button", "name": "Continue"}],
        "Applicant Jane Roe - https://carrier.example/apply")
    assert len(sig) == 64 and all(ch in "0123456789abcdef" for ch in sig)
    for leaked in ("jane", "roe", "continue", "carrier", "apply", "http"):
        assert leaked not in sig


# ── The explorer now emits it without an oracle ─────────────────────────

def test_tier1_advance_carries_a_signature(tmp_path):
    """THE HEADLINE. No oracle configured, no LLM reachable — and the
    deterministic pick still comes back with the key it will be remembered
    under."""
    c = _build(tmp_path)
    controls = [_btn("Back"), _btn("Continue")]
    d = _pick(c, controls)
    assert d.tier == 1 and d.control["name"] == "Continue"
    assert d.signature == compute_signature(
        c._tier3_candidates(controls), "Step")
    assert d.signature


def test_tier2_destination_advance_carries_a_signature(tmp_path):
    c = _build(tmp_path)
    controls = [_btn("Back"), _btn("Continue to Payment")]
    d = _pick(c, controls)
    assert d.tier == 2 and d.control["name"] == "Continue to Payment"
    # "Continue to Payment" is commit-labelled, so it is NOT oracle-eligible:
    # remembering it would write an answer recall is forbidden to give back.
    assert d.signature == ""


def test_every_tier2_pick_is_structurally_unrecallable(tmp_path):
    """Not an accident of one label — a LAW of the tier, stated so nobody
    later "fixes" tier 2 into contributing commit words to the shared pool.

    Tier 1 already takes any destination-shaped label that carries no commit
    word ("Continue to Beneficiary Details"), so tier 2 fires ONLY on the shape
    tier 1 vetoed: advance word + destination + commit word. Every such label is
    filtered out of the oracle-eligible set, so it can never be handed back by
    recall — which is exactly why it is never written."""
    c = _build(tmp_path)
    for label in ("Continue to Payment", "Next: Confirm Order",
                  "Proceed to Checkout"):
        d = _pick(c, [_btn("Back"), _btn(label)])
        assert d.tier in (0, 2), f"{label!r} reached tier {d.tier}"
        if d.tier == 2:
            assert _WIZARD_COMMIT_RE.search(d.control["name"]), (
                f"{label!r} reached tier 2 without a commit word — the "
                f"exclusion below now hides a recallable advance")
            assert d.signature == ""


def test_the_signature_is_computed_over_the_set_the_oracle_would_have_seen(tmp_path):
    """If it were computed over the page inventory instead, the key would never
    match the one qe-central computes and the memory would be unreachable."""
    c = _build(tmp_path)
    controls = [
        _btn("Continue"),
        _btn("Delete Account", danger=True),      # filtered: danger
        _btn("Save Later", disabled=True),        # filtered: disabled
        _btn(""),                                 # filtered: nameless
        _btn("Submit Application"),               # filtered: commit word
    ]
    d = _pick(c, controls)
    assert d.tier == 1
    assert d.signature == compute_signature(
        [{"kind": "button", "name": "Continue"}], "Step")


def test_an_advance_no_tier_found_carries_no_signature(tmp_path):
    """Tier 0 decided nothing; a key would claim a proof that does not exist."""
    c = _build(tmp_path)
    d = _pick(c, [_btn("Back"), _btn("Print")])
    assert d.tier == 0 and d.signature == ""


def test_tier3_still_uses_the_oracles_own_signature(tmp_path):
    """The oracle's reply stays authoritative for its own picks — this change
    adds a producer, it does not replace one."""
    async def oracle(candidates, page_title, page_url):
        return {"index": 0, "status": ORACLE_PICKED, "signature": "from-the-oracle"}

    c = _build(tmp_path, oracle=oracle)
    d = _pick(c, [_btn("See My Quote")])
    assert d.tier == 3 and d.signature == "from-the-oracle"

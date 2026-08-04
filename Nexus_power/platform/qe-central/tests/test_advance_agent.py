"""advance_agent — the crawl-time agent that decides which control advances a
multi-step flow.

SAFETY: commit-labeled, danger, disabled and nameless controls are dropped
server-side BEFORE the prompt is built and the pick is re-mapped to the
caller's original indices — a commit pick can never leave this module,
whoever calls it (defense in depth under the explorer's own filter).

HONESTY: three-state outcome. ``none`` is the agent's honest "nothing here
advances"; every failure to decide — LLM error, unreadable reply,
out-of-range pick — is ``unavailable``, because a reply we could not read is
not a reply that said stop.
"""
from __future__ import annotations

import asyncio
import types

from app.services import advance_agent
from app.services.advance_agent import (
    STATUS_NONE,
    STATUS_PICKED,
    STATUS_UNAVAILABLE,
    _COMMIT_VETO_RE,
    _NUM_RE,
    _build_prompt,
    compute_signature,
    eligible_controls,
)


import pytest

from app.services import advance_memory


@pytest.fixture(autouse=True)
def _no_db_recall(monkeypatch):
    """pick_advance consults advance memory before the LLM; unit tests must
    never touch a real database — recall misses by default (tests that WANT
    a hit re-patch)."""

    async def miss(*a, **k):
        return None

    monkeypatch.setattr(advance_memory, "recall", miss)
    monkeypatch.setattr(advance_memory, "recall_prior", miss)


def _llm(monkeypatch, *, ok=True, text="", detail=""):
    """Monkeypatch platform_api.complete_llm with a scripted reply, recording
    the prompt it was given."""
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return types.SimpleNamespace(ok=ok, text=text, detail=detail)

    monkeypatch.setattr(advance_agent.platform_api, "complete_llm", fake)
    return seen


def _pick(controls, title="Health Questions", url="https://app.example/quote/health"):
    return asyncio.run(advance_agent.pick_advance(
        tenant_id="t1", controls=controls, page_title=title, page_url=url))


# ── SAFETY: the server-side eligibility filter ───────────────────────────

def test_eligible_drops_commit_danger_disabled_nameless():
    controls = [
        {"name": "Sign & Submit Application", "kind": "button"},   # commit
        {"name": "Pay Now", "kind": "button"},                     # commit
        {"name": "Delete", "kind": "button", "danger": True},      # danger
        {"name": "Later", "kind": "button", "disabled": True},     # disabled
        {"name": "", "kind": "button"},                            # nameless
        {"name": "See My Quote", "kind": "button"},                # eligible
        {"name": "Skip intro", "kind": "link"},                    # eligible
    ]
    pairs = eligible_controls(controls)
    assert [(i, c["name"]) for i, c in pairs] == [
        (5, "See My Quote"), (6, "Skip intro")]


def test_picked_index_maps_to_callers_original_list(monkeypatch):
    """The LLM numbers the FILTERED list; the caller gets an index into the
    ORIGINAL list."""
    _llm(monkeypatch, text="1")
    controls = [
        {"name": "Pay Now", "kind": "button"},        # filtered out
        {"name": "See My Quote", "kind": "button"},   # eligible #1
    ]
    d = _pick(controls)
    assert d.status == STATUS_PICKED
    assert d.index == 1
    assert controls[d.index]["name"] == "See My Quote"


def test_prompt_never_contains_commit_controls(monkeypatch):
    seen = _llm(monkeypatch, text="1")
    _pick([
        {"name": "Sign & Submit Application", "kind": "button"},
        {"name": "See My Quote", "kind": "button"},
    ])
    assert "Sign & Submit Application" not in seen["prompt"]
    assert '"See My Quote"' in seen["prompt"]


def test_all_commit_controls_is_honest_none(monkeypatch):
    """Everything forward commits → nothing here ADVANCES. No LLM call."""
    called = {"n": 0}

    async def fake(**kw):
        called["n"] += 1
        return types.SimpleNamespace(ok=True, text="1", detail="")

    monkeypatch.setattr(advance_agent.platform_api, "complete_llm", fake)
    d = _pick([{"name": "Sign & Submit Application", "kind": "button"},
               {"name": "Pay Now", "kind": "button"}])
    assert d.status == STATUS_NONE
    assert d.index is None
    assert called["n"] == 0


def test_system_prompt_forbids_commit_picks():
    text = advance_agent.SYSTEM.lower()
    assert "commit" in text
    assert "reply 0" in text


def test_advance_vocabulary_parity_pin():
    """The advance union must mirror the explorer's pack (see the MIRROR LAW
    in app/services/advance_vocab.py)."""
    from app.services import advance_vocab
    assert advance_vocab.ADVANCE_RE.pattern == \
        r"\b(next|continue|proceed|forward)\b"


def test_commit_vocabulary_parity_pin():
    """MUST stay pattern-identical to the explorer's ``_WIZARD_COMMIT_RE``
    (qe-explorer/app/crawler.py). Mirrored pin: qe-explorer/tests/
    test_e2e_advance.py::test_commit_vocabulary_parity_pin — change BOTH or
    neither."""
    assert _COMMIT_VETO_RE.pattern == (
        r"\b(submit|send|pay|paying|paid|payment|payments|buy|buying|purchase|"
        r"purchasing|order|checkout|check\s*out|place\s*order|confirm|finish|"
        r"complete|done|agree|accept|sign|book|reserve|schedule|activate|create|"
        r"register|subscribe|delete|cancel|remove|apply)\b"
    )


# ── HONESTY: the three-state outcome ─────────────────────────────────────

def test_llm_zero_is_honest_none(monkeypatch):
    _llm(monkeypatch, text="0")
    d = _pick([{"name": "Back", "kind": "button"}])
    assert d.status == STATUS_NONE
    assert d.signature  # the decision point still has a signature


def test_llm_failure_is_unavailable(monkeypatch):
    _llm(monkeypatch, ok=False, detail="429 rate limited")
    d = _pick([{"name": "See My Quote", "kind": "button"}])
    assert d.status == STATUS_UNAVAILABLE


def test_unreadable_reply_is_unavailable_not_none(monkeypatch):
    _llm(monkeypatch, text="I cannot determine that")
    d = _pick([{"name": "See My Quote", "kind": "button"}])
    assert d.status == STATUS_UNAVAILABLE


def test_out_of_range_pick_is_unavailable(monkeypatch):
    _llm(monkeypatch, text="7")
    d = _pick([{"name": "See My Quote", "kind": "button"}])
    assert d.status == STATUS_UNAVAILABLE


def test_empty_controls_is_unavailable(monkeypatch):
    _llm(monkeypatch, text="1")
    d = _pick([])
    assert d.status == STATUS_UNAVAILABLE


# ── The value-free decision-point signature ──────────────────────────────

def test_signature_is_stable_and_order_insensitive():
    a = [(0, {"name": "See My Quote", "kind": "button"}),
         (1, {"name": "Back", "kind": "button"})]
    b = [(4, {"name": "Back", "kind": "button"}),
         (9, {"name": "see my quote  ", "kind": "button"})]
    assert compute_signature(a, "Health Questions") == \
        compute_signature(b, "Health Questions")


def test_signature_contains_no_url_or_host_material():
    """The signature basis is names + kinds + title shape ONLY — the page URL
    cannot influence it because the builder never receives one (URL-guard
    doctrine for learned artifacts)."""
    import inspect
    params = list(inspect.signature(compute_signature).parameters)
    assert params == ["eligible", "page_title"]


def test_signature_differs_by_title_shape():
    e = [(0, {"name": "Go", "kind": "button"})]
    assert compute_signature(e, "Health Questions") != \
        compute_signature(e, "Payment Details")


def test_picked_carries_signature(monkeypatch):
    _llm(monkeypatch, text="1")
    d = _pick([{"name": "See My Quote", "kind": "button"}])
    assert d.status == STATUS_PICKED and len(d.signature) == 64


# ── Prompt construction ──────────────────────────────────────────────────

def test_prompt_includes_all_eligible_controls(monkeypatch):
    seen = _llm(monkeypatch, text="1")
    _pick([
        {"name": "See My Quote", "kind": "button"},
        {"name": "Back", "kind": "button"},
        {"name": "Home", "kind": "link"},
    ])
    p = seen["prompt"]
    assert "Page: Health Questions" in p
    assert '1. "See My Quote" (button)' in p
    assert '2. "Back" (button)' in p
    assert '3. "Home" (link)' in p


def test_prompt_strips_query_params():
    p = _build_prompt(
        [{"name": "Go", "kind": "button"}],
        "Step 3", "https://app.example/apply/step3?session=abc123&token=secret")
    assert "session=" not in p and "token=" not in p
    assert "URL path: https://app.example/apply/step3" in p


def test_response_parsing_picks_number():
    assert _NUM_RE.search("3").group() == "3"
    assert _NUM_RE.search("The answer is 2.").group() == "2"
    assert _NUM_RE.search("1\n").group() == "1"


def test_response_parsing_no_number():
    assert _NUM_RE.search("none") is None
    assert _NUM_RE.search("") is None

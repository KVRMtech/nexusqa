"""The operator chose LLM data fill, and the crawl never heard about it.

MEASURED on orangehrm 2026-09-04. A Target-mode crawl configured with
``data_mode: "llm"`` — the portal's own name for "let the model answer the
fields" — ran to completion and reported:

    qec.crawl.event {"event": "crawl_tokens", "llm_calls": 0,
                     "total_tokens": 0, "estimated_cost_usd": 0.0}

Zero calls. Of the eleven fields it filled, eight received the deterministic
placeholder ``autotest``, two of those being date-range inputs that cannot
accept text at all. Scored against the fields the pages actually contain the
crawl found 12 of 12 (100%) and filled 11 of 12 (92%), but only 3 of 11 values
were the kind of data the field asks for — 27%.

THE SEAM. qe-central resolves the choice correctly
(``services/data_posture.resolve("llm") -> data_llm=True``) and sends it on every
dispatch. ``ExploreRequest`` had no field to receive it, so pydantic dropped it,
and ``_llm_data_agent`` consulted only ``settings.data_llm`` — the container-wide
``QEC_DATA_LLM``, which is **unset on the fleet**. Two correct halves, one
feature that could never run for any application, whatever the client selected.

WHY THE LAST TEST IS THE POINT. The flag guards a call to a THIRD-PARTY MODEL
with the application's own field text. discovery.py states the rule it is
keeping: "a crawl must never start consulting a third-party model because nobody
chose anything — that is a decision, and an unmade decision is not one." Wiring
the operator's YES must not also wire a default yes.
"""
from __future__ import annotations

import pytest

from app.main import ExploreRequest


class _Settings:
    """Stands in for app.config.Settings — only the flags this path reads."""

    def __init__(self, data_llm=False):
        self.data_llm = data_llm
        self.data_llm_model = "test-model"
        self.data_llm_max_calls = 7


class _Probe:
    """The smallest object carrying the two inputs `_llm_data_agent` reads."""

    def __init__(self, *, per_crawl, settings_flag):
        self._data_llm = per_crawl
        self._branch_settings = _Settings(settings_flag)
        self.crawl_id = "c1"
        self.tenant_id = "t1"

    _llm_data_agent = None  # bound below


def _bind():
    from app.discovery import DiscoveryMixin
    _Probe._llm_data_agent = DiscoveryMixin._llm_data_agent


_bind()


def _minimal_request(**over):
    body = {"crawl_id": "c1", "tenant_id": "t1",
            "target_url": "https://app.example/start"}
    body.update(over)
    return ExploreRequest(**body)


# ══════════════════════════════════════════════════════════════════════════
#  The dispatch carries the choice
# ══════════════════════════════════════════════════════════════════════════

def test_the_request_accepts_the_flag_qe_central_sends():
    """The measured drop: the field did not exist, so the value vanished."""
    req = _minimal_request(data_mode="agent", data_llm=True)
    assert req.data_llm is True, (
        "ExploreRequest silently discarded data_llm — qe-central resolves the "
        "operator's choice and sends it on every dispatch, and this is where it "
        "was lost"
    )


def test_the_flag_defaults_off_when_the_dispatch_says_nothing():
    """CONTROL — a dispatch that carries no choice must not turn the model on."""
    assert _minimal_request().data_llm is False


# ══════════════════════════════════════════════════════════════════════════
#  The crawl acts on it
# ══════════════════════════════════════════════════════════════════════════

def test_the_per_crawl_choice_enables_the_agent_without_the_env_var():
    """The fix. QEC_DATA_LLM is unset on the fleet; the operator's YES must win."""
    agent = _Probe(per_crawl=True, settings_flag=False)._llm_data_agent()
    assert agent is not None, (
        "the operator chose LLM data fill and the agent was still not built — "
        "this is the state that produced llm_calls=0 on a crawl configured for it"
    )


def test_the_deployment_default_still_works():
    """CONTROL — backward compatibility. A fleet that sets QEC_DATA_LLM keeps it."""
    agent = _Probe(per_crawl=False, settings_flag=True)._llm_data_agent()
    assert agent is not None


def test_neither_flag_means_no_model_is_consulted():
    """CONTROL, and the one that must never stop passing.

    The flag guards sending the application's own field text to a third-party
    model. Nobody choosing is NOT the same as somebody choosing yes. If this
    test fails, the fix has quietly turned a paid, PII-adjacent egress on for
    every crawl in the fleet.
    """
    agent = _Probe(per_crawl=False, settings_flag=False)._llm_data_agent()
    assert agent is None, (
        "the data agent was built although neither the operator nor the "
        "deployment asked for it"
    )


@pytest.mark.parametrize("falsey", [None, 0, "", False])
def test_a_falsey_per_crawl_value_is_not_a_yes(falsey):
    """CONTROL — a missing value must not read as consent through truthiness."""
    probe = _Probe(per_crawl=False, settings_flag=False)
    probe._data_llm = falsey
    assert probe._llm_data_agent() is None


def test_the_crawler_stores_what_the_request_carried():
    """CONTROL against a half-wired fix.

    main.py can accept the field and still fail to hand it to the Crawler — the
    shape of the first name_attr attempt, which added a capture and a locator
    rung and changed nothing because refinement dropped the value in between.
    """
    import inspect
    from app.crawler import Crawler
    assert "data_llm" in inspect.signature(Crawler.__init__).parameters, (
        "Crawler does not accept data_llm, so ExploreRequest can carry the "
        "operator's choice no further than the request object"
    )
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
    assert "data_llm=bool(req.data_llm)" in src.read_text(encoding="utf-8"), (
        "main.py never passes the flag to the Crawler it builds"
    )

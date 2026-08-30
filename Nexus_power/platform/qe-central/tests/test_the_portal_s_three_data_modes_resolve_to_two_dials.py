"""THE PORTAL'S THREE DATA MODES RESOLVE TO THE EXPLORER'S TWO DIALS.

The portal offers "LLM", "User" and "User + LLM". The explorer takes two
separate dials — ``data_mode`` (does the deterministic generator answer semantic
choices?) and ``data_llm`` (is rung 8 consulted at all?). Three modes across two
dials is precisely the shape that drifts when it lives as an ``if`` in a router:
a caller sets the mode, forgets the flag, and a crawl runs with the model off
while its report says "LLM".

These tests pin the mapping, and pin the two properties that make the mapping
safe: an unmade choice never turns the model on, and an explicit choice is never
overridden by the environment's default.
"""
from __future__ import annotations

import pytest

from app.services.data_posture import (EXPLORER_AGENT, EXPLORER_USER, MODE_LLM,
                                       MODE_USER, MODE_USER_LLM, PORTAL_MODES,
                                       resolve)


# ── the three modes ────────────────────────────────────────────────────────

def test_llm_mode_answers_everything_it_safely_can():
    p = resolve(MODE_LLM, attested_default_agent=False)
    assert (p.data_mode, p.data_llm) == (EXPLORER_AGENT, True)


def test_user_mode_is_the_behaviour_every_crawl_had_before_rung_8():
    p = resolve(MODE_USER, attested_default_agent=True)
    assert (p.data_mode, p.data_llm) == (EXPLORER_USER, False)


def test_user_plus_llm_lets_the_client_s_data_win_and_fills_the_rest():
    """The generator still declines semantic choices — that is what makes the
    client's own data win — and rung 8 fills what is left."""
    p = resolve(MODE_USER_LLM, attested_default_agent=False)
    assert (p.data_mode, p.data_llm) == (EXPLORER_USER, True)


# ── the properties that make it safe ───────────────────────────────────────

def test_an_unmade_choice_never_turns_the_model_on():
    """THE ONE THAT MATTERS. Consulting a third-party model is a decision, and
    an unmade decision is not one."""
    for attested in (True, False):
        assert resolve("", attested_default_agent=attested).data_llm is False


def test_an_undeclared_mode_follows_the_environment_s_attested_default():
    assert resolve("", attested_default_agent=True).data_mode == EXPLORER_AGENT
    assert resolve("", attested_default_agent=False).data_mode == EXPLORER_USER


def test_an_explicit_choice_is_never_overridden_in_either_direction():
    """An operator who chose "user" on an attested environment still gets user;
    one who chose "llm" on an unattested one still gets the model — what the
    ENVIRONMENT permits is prod_guard's decision, not this module's."""
    assert resolve(MODE_USER, attested_default_agent=True).data_mode == EXPLORER_USER
    assert resolve(MODE_LLM, attested_default_agent=False).data_llm is True


def test_an_unknown_mode_falls_back_rather_than_raising():
    """A portal that sends something new must degrade, not 500 a dispatch."""
    p = resolve("turbo", attested_default_agent=True)
    assert p.data_llm is False
    assert p.data_mode == EXPLORER_AGENT


def test_case_and_padding_do_not_change_the_decision():
    assert resolve("  LLM  ", attested_default_agent=False).data_llm is True


# ── what the operator is told ──────────────────────────────────────────────

@pytest.mark.parametrize("mode", PORTAL_MODES)
def test_every_mode_can_explain_itself_in_a_sentence(mode):
    summary = resolve(mode, attested_default_agent=False).summary
    assert summary and len(summary) < 200


def test_the_summary_distinguishes_the_two_llm_modes():
    a = resolve(MODE_LLM, attested_default_agent=False).summary
    b = resolve(MODE_USER_LLM, attested_default_agent=False).summary
    assert a != b, "a report must not describe both modes the same way"


# ── the dial operators already set ─────────────────────────────────────────

def test_the_legacy_agent_dial_is_passed_through_not_discarded():
    """Schedules written before the portal had modes carry the explorer's own
    value. "agent" has always meant "the generator answers choices" and nothing
    about a model — dropping it would silently downgrade a configured crawl."""
    p = resolve("agent", attested_default_agent=False)
    assert (p.data_mode, p.data_llm) == (EXPLORER_AGENT, False)


def test_the_legacy_dial_still_does_not_turn_the_model_on():
    assert resolve("agent", attested_default_agent=True).data_llm is False

"""A page's headings are not ours to send to a third party.

MEASURED 2026-09-05, with the real capture JS in Chromium and the real egress
guard. Once ``sectionOf`` began reading the group label above a field — the fix
that lets a crawler know "From" and "To" are dates — the same mechanism started
returning the identity of whatever record the page was showing:

    'Date of Birth' -> 'Application for Jane Q. Doe'
    'Percent'       -> 'Beneficiary: Robert Alan Smith (DOB 11/02/1958)'
    'Weight'        -> 'Patient John Smith, MRN 004512, DOB 1948-03-02'

and every one of those passed ``nexus_sdk.llm.pii_guard.check_text`` untouched.
That guard is deliberately narrow — email, +E.164 phone, US SSN, Luhn card,
IBAN, AWS key, GitHub token — so a person's name, a date of birth, a policy or
member or MRN number, and a phone number written the way people write them are
all outside it. Nothing else on the path from capture to provider redacts.

``services/pii_egress_guard`` rests its entire safety argument on the payload
being "VALUE-FREE — only labels/types/options". Sending a record page's heading
makes that false.

WHY THIS WAS LATENT AND NOT NEW. ``forms.py`` had always passed ``section``; it
simply never carried anything, on 0 of 19,838 filled fields ever recorded. The
capture fix is what would have switched the path on.

WHAT IS KEPT. The section is still captured. It is evidence, it stays on the
box, and it is exactly the signal a LOCAL classifier needs to type a date range
correctly. Deciding a field's type does not require telling anyone else what the
page says.
"""
from __future__ import annotations

import inspect
import re

from app import forms


def _call_site() -> str:
    """The text of the ONE place the fill path asks the model for a value."""
    src = inspect.getsource(forms).splitlines()
    start = next((i for i, l in enumerate(src) if "llm.value_for(" in l), None)
    assert start is not None, (
        "the llm.value_for call site is gone or renamed — this test pins what "
        "that call may carry, so it must be found, not silently skipped"
    )
    # Balance the parens rather than regex them: the arguments contain calls of
    # their own, and a non-greedy match stops inside verdict.get("type") — which
    # made an earlier version of this test read a fragment and fail on `kind=`
    # that was plainly there.
    depth, out = 0, []
    for line in src[start:start + 12]:
        out.append(line)
        depth += line.count("(") - line.count(")")
        if depth <= 0 and out:
            break
    return chr(10).join(out)


def test_the_model_is_still_asked_for_values():
    """CONTROL FIRST — the assertion below is worthless if nothing calls the model.

    Without this, deleting the whole rung would make every other test in this
    file pass, which is the shape of blind verifier this repository keeps
    finding.
    """
    args = _call_site()
    assert "name=" in args and "semantic_type=" in args, (
        "the call site no longer passes the field itself; the rung is gone, not "
        "narrowed"
    )


def test_the_section_is_not_sent():
    """The fix: the page's own heading text stays on the box."""
    args = _call_site()
    assert "section=" not in args, (
        "forms.py is handing the page's heading to the model again. On a record "
        "page that heading IS the record's identity — a name, a date of birth, "
        "a policy or MRN number — and the egress guard does not detect any of "
        "them. Args were: %r" % args.strip()
    )


def test_what_it_may_still_send_is_the_field_itself():
    """The narrowing must be exactly this, not a wholesale gutting.

    The label, the type, the kind, the app's own declared options and its own
    declared constraints are the application describing the QUESTION. That is
    what the value agent exists to answer and none of it is a record's data.
    """
    args = _call_site()
    for allowed in ("name=", "semantic_type=", "kind=", "options=", "constraints="):
        assert allowed in args, (
            "%s was dropped from the value request — the model can no longer "
            "answer accurately, which is a different defect from the one being "
            "fixed" % allowed
        )


def test_the_agent_defaults_section_to_empty():
    """CONTROL — the parameter still exists, so a future caller could pass it.

    Keeping the signature is deliberate: llm_data.value_for is a general wire
    and a caller with sanitised context may legitimately use it. What must never
    happen again is the crawl path passing raw page text by default.
    """
    from app.llm_data import LLMDataAgent
    sig = inspect.signature(LLMDataAgent.value_for)
    assert sig.parameters["section"].default == "", (
        "section must default to empty, or omitting it at the call site stops "
        "being the same as not sending it"
    )

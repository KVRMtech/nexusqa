"""RUNG 7.5 — THE PUBLISHED TEST CREDENTIALS OF AN EMBEDDED SERVICE.

WHY THIS EXISTS. Most modern financial funnels embed somebody else's widget at
the moment that matters — Stripe for the card, Plaid to link a bank, DocuSign
to sign, a captcha to prove you are a person. The crawl reaches the step and
stops, because the persona generator has no idea what Plaid wants and a model
inventing a bank login is inventing a credential.

Every one of those services publishes sandbox credentials for exactly this
situation, and the values are conventions rather than secrets: Stripe's
``4242 4242 4242 4242``, Plaid's ``user_good`` / ``pass_good``, Google's
reCAPTCHA test key. Knowing them is the difference between a funnel that
completes in a test environment and one that dead-ends at its third-party step.

KEYED ON THE FRAME'S ORIGIN, WHICH IS THE ONE HONEST SIGNAL. A control inside
``js.stripe.com`` is a Stripe control, whatever the surrounding page calls it.
Matching on a label instead would guess — "Card number" appears on a page that
posts to its own gateway just as readily.

NOT SECRETS, AND NOT A BACKDOOR. Every value here is published by its vendor as
test data, works only against that vendor's sandbox, and moves no money. A
production endpoint rejects all of them, which is the property that makes this
safe to ship: if the environment is real, the values simply fail.

MEASURED (all crawls to 2026-08-30): ``frame_origin`` is captured on every
control and was ``""`` in all 1,311 observed actions — no application crawled so
far embedded a third party. The registry is therefore proven against a fixture
rather than a live funnel, and this file says so rather than implying coverage
it has not earned.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

#: origin fragment -> {semantic type or label pattern: published test value}.
#:
#: Fragments, not exact hosts: vendors move between ``js.stripe.com`` and
#: ``checkout.stripe.com`` without changing what the widget wants.
SANDBOX_VALUES: dict[str, dict[str, str]] = {
    "stripe.com": {
        "card_number": "4242424242424242",
        "card_expiry": "12/34",
        "card_cvc": "123",
        "postal_code": "42424",
    },
    "plaid.com": {
        "username": "user_good",
        "password": "pass_good",
        "otp": "1234",
    },
    "docusign": {
        # DocuSign's demo signing accepts any typed name; the persona's own is
        # the coherent choice, so nothing is overridden here.
    },
    "recaptcha": {
        # Google's published always-passing test response.
        "captcha": "test",
    },
}

#: Label patterns used ONLY to pick which value within an already-matched
#: origin — never to decide that a control belongs to a service.
_LABEL_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(card|pan)\b.*\bnumber\b|\bnumber\b.*\bcard\b", "card_number"),
    (r"\bexp(ir\w*)?\b|\bmm\s*/\s*yy\b", "card_expiry"),
    (r"\bcvc\b|\bcvv\b|\bsecurity code\b", "card_cvc"),
    (r"\buser(name)?\b|\blogin\b|\bemail\b", "username"),
    (r"\bpass(word)?\b", "password"),
    (r"\bcode\b|\botp\b", "otp"),
    (r"\bpost(al)?\s*code\b|\bzip\b", "postal_code"),
)


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def service_for(frame_origin: str) -> Optional[str]:
    """The registry key for this frame's origin, or None.

    A control on the application's OWN page has an empty origin and is never a
    third-party control, which is why "" can never match.
    """
    origin = _norm(frame_origin)
    if not origin:
        return None
    for fragment in SANDBOX_VALUES:
        if fragment in origin:
            return fragment
    return None


def value_for(control: Mapping[str, Any], *, semantic_type: str = "") -> Optional[str]:
    """The published sandbox value for this control, or None.

    Resolution is origin-first: without a recognised third-party frame there is
    no answer here at all, whatever the control is called.
    """
    service = service_for(str(control.get("frame_origin") or ""))
    if service is None:
        return None
    values = SANDBOX_VALUES.get(service) or {}
    if not values:
        return None

    # The semantic type the classifier already decided wins — it read the
    # application's own declarations, which is stronger evidence than a label.
    key = _norm(semantic_type)
    if key and key in values:
        return values[key]

    label = _norm(control.get("question_label") or control.get("name") or "")
    if not label:
        return None
    for pattern, field in _LABEL_HINTS:
        if field in values and re.search(pattern, label):
            return values[field]
    return None

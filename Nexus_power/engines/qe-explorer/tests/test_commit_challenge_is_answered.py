"""A COMMIT THAT ASKS FOR A SECOND FACTOR IS STILL THE COMMIT WE APPROVED.

Once a challenge modal stopped counting as a confirmation (see
test_challenge_dialog_is_not_a_receipt), a real gap became visible that the
false verdict had been hiding: the crawl OPENS the modal and cannot ANSWER it,
so the approved commit never completes.

MEASURED, on two independent applications:

  * LifeOps (client, 2026-08-27) — clicking the granted control `Sign` opens
    "Sign Electronic Delivery Consent — PIN confirmation is required to create
    an auditable electronic signature event", carrying one required field
    (label "Confirm PIN", type=password, inputmode=numeric, maxlength=6) and
    the buttons ["Sign document", "Cancel"]. The crawl recorded
    `crossed but NOT verified: the far side was not a confirmation` and stopped.
    The three documents gate every other screen in the application.

  * acme-life (proving ground) — the same shape: two buttons named "Bind
    policy", one that only OPENS the modal and one INSIDE it that binds.
    `POLICY BOUND · Confirmation #AL-…` has never been reached by any crawl.

WHY ANSWERING IT IS NOT A NEW PERMISSION. The operator approved crossing this
named control, and `gate_submit` already ran. A re-authentication challenge is
part of that same commit, not a second one: the modal is the application
asking us to prove we meant the click it has already accepted. So the answer is
bounded to the dialog the approved click opened, uses the secret the operator
ALREADY supplied for this tenant, and is attempted once.

The secret is never invented. With no credentials configured there is nothing
to answer with, and the crawl stops exactly where it does today — which is the
control below.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from app.browser import RawObservation
from test_approved_submit_crossing import _Port, build_crawler, ctl


class _ChallengePort(_Port):
    """Clicking the commit opens a re-auth modal; answering it completes."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.answered_with: list[str] = []

    async def click(self, control):
        self._clicked = True
        self.clicks.append(str(control.get("name") or ""))
        return RawObservation(
            url_before=self._url, url_after=self._url,
            dialog_opened=True, dialog_is_challenge=True,
            dialog_detail="Sign Electronic Delivery Consent",
        )

    async def answer_challenge_dialog(self, secret: str):
        self.answered_with.append(secret)
        return RawObservation(
            url_before=self._url, url_after=self._url,
            dialog_opened=False,
            confirmation_detail="Document signed. Reference DOC-2026-00311.",
        )


def _port(**kw) -> _ChallengePort:
    return _ChallengePort(controls=[ctl("Sign"), ctl("Cancel")],
                          texts=["Electronic Delivery Consent"], **kw)


def _cross(c, name="Sign"):
    return asyncio.run(c._execute_approved_submit(
        name=name, control=ctl(name), url="https://app.example/documents",
        fingerprint="fp-docs", depth=0, renavigate=False))


def test_the_approved_commit_completes_through_its_own_challenge(tmp_path):
    """THE GAP. With the operator's secret available, the modal the approved
    click opened is answered and the commit reaches its confirmation."""
    port = _port()
    c = build_crawler(tmp_path, grants=[{"control": "Sign"}], port=port)
    c._credentials = types.SimpleNamespace(password="2468")
    assert _cross(c) is True
    assert port.answered_with == ["2468"], (
        "the approved commit's own challenge was never answered")


def test_no_credentials_means_no_answer_and_no_invented_secret(tmp_path):
    """THE CONTROL. A secret is never fabricated. With nothing configured the
    crawl stops at the modal exactly as it does today — an honest halt, not a
    guess at somebody's PIN."""
    port = _port()
    c = build_crawler(tmp_path, grants=[{"control": "Sign"}], port=port)
    _cross(c)
    assert port.answered_with == []

"""A PAGE STILL DECIDING WHAT IT IS GETS READ AGAIN.

MEASURED ON THE LIVE vkpowerlife FUNNEL, 2026-08-30. Its underwriting step
renders "Processing Your Application" and reveals "Continue to Payment" 2.2
SECONDS later, on a client-side timer:

    networkidle   670ms   0 fields, 0 buttons
    ...
    forward ctrl 2857ms   "Continue to Payment", "Back"

``networkidle`` was correct — there was no network call to wait for — so the
crawl catalogued a state offering nothing and moved on. The funnel stopped at
step 6 of 10, NOTHING was recorded as blocking it (there was nothing to click),
and ``forms_confirmed`` stayed 0. Every async decision step in a financial
funnel has exactly this shape, and it is the reason a crawl covers the front of
an application and not the flows behind it.

THE TRIGGER IS A STATE THAT OFFERS NOTHING, which is what makes the fix free: a
page holding a field or a button has already decided what it is and returns
immediately, so the wait is spent only where the alternative was recording
nothing at all. These tests hold that line in both directions — the page that
must wait, and the pages that must not.
"""
from __future__ import annotations

import pytest


class _Port:
    """A page that reveals its forward control only after it is waited on."""

    def __init__(self, before, after, *, reveal_after_ms=3000):
        self.before, self.after = before, after
        self.reveal_after_ms = reveal_after_ms
        self.waited_ms = 0
        self.reads = 0

    async def sleep_ms(self, ms):
        self.waited_ms += int(ms)

    async def collect_controls(self):
        self.reads += 1
        return (self.after if self.waited_ms >= self.reveal_after_ms
                else self.before)


def _processing():
    """The state vkpowerlife actually presented at 670ms."""
    return [{"kind": "link", "name": "VKPower Life Insurance"}]


def _decided():
    """The same state at 2857ms."""
    return [{"kind": "link", "name": "VKPower Life Insurance"},
            {"kind": "button", "name": "Continue to Payment"},
            {"kind": "button", "name": "Back"}]


async def _settle(discovery, port, controls):
    class _Obs:
        url = "http://app.test/apply/decision/"
    return await discovery._settle_undecided_page(_Obs(), controls)


@pytest.mark.asyncio
async def test_the_processing_page_is_read_again_and_yields_its_forward_control():
    """THE ONE THAT MATTERS. Without this the funnel stops at step 6 of 10."""
    from app.discovery import DiscoveryMixin

    class _D(DiscoveryMixin):
        pass

    d = _D()
    port = _Port(_processing(), _decided())
    d._port = port
    d._tracker = type("T", (), {"note_request": lambda s: None})()
    d._refuse_pack = None

    got = await _settle(d, port, _processing())
    names = [c.get("name") for c in got]
    assert "Continue to Payment" in names
    assert port.waited_ms >= 2857, "it must outwait the measured 2.2s gap"


@pytest.mark.asyncio
async def test_a_page_that_already_has_a_button_is_never_waited_on():
    """FALSIFICATION CONTROL, and the one that keeps the fix free. If this
    passed while waiting, every page in every crawl would pay the settle."""
    from app.discovery import DiscoveryMixin

    class _D(DiscoveryMixin):
        pass

    d = _D()
    port = _Port(_decided(), _decided())
    d._port = port
    d._tracker = type("T", (), {"note_request": lambda s: None})()
    d._refuse_pack = None

    await _settle(d, port, _decided())
    assert port.waited_ms == 0
    assert port.reads == 0, "not even one extra browser round trip"


@pytest.mark.asyncio
async def test_a_page_with_a_field_is_never_waited_on_either():
    from app.discovery import DiscoveryMixin

    class _D(DiscoveryMixin):
        pass

    d = _D()
    port = _Port([], [])
    d._port = port
    d._tracker = type("T", (), {"note_request": lambda s: None})()
    d._refuse_pack = None

    form = [{"kind": "text", "name": "First Name"}]
    assert await _settle(d, port, form) == form
    assert port.waited_ms == 0


@pytest.mark.asyncio
async def test_a_genuinely_empty_page_is_reported_as_it_was_first_seen():
    """The wait is spent once on a state the crawl could not have used anyway,
    and the crawl carries on rather than treating the re-read as authoritative."""
    from app.discovery import DiscoveryMixin

    class _D(DiscoveryMixin):
        pass

    d = _D()
    port = _Port(_processing(), _processing())
    d._port = port
    d._tracker = type("T", (), {"note_request": lambda s: None})()
    d._refuse_pack = None

    got = await _settle(d, port, _processing())
    assert [c.get("name") for c in got] == ["VKPower Life Insurance"]


@pytest.mark.asyncio
async def test_a_port_that_cannot_be_re_read_reports_what_it_first_saw():
    """A page that fails mid-settle must not lose the state already captured."""
    from app.discovery import DiscoveryMixin

    class _Broken(_Port):
        async def collect_controls(self):
            raise RuntimeError("the page navigated away underneath us")

    class _D(DiscoveryMixin):
        pass

    d = _D()
    d._port = _Broken(_processing(), _decided())
    d._tracker = type("T", (), {"note_request": lambda s: None})()
    d._refuse_pack = None

    got = await _settle(d, d._port, _processing())
    assert [c.get("name") for c in got] == ["VKPower Life Insurance"]


@pytest.mark.asyncio
async def test_a_port_with_no_wait_primitive_degrades_rather_than_raising():
    from app.discovery import DiscoveryMixin

    class _D(DiscoveryMixin):
        pass

    d = _D()
    d._port = object()
    d._tracker = type("T", (), {"note_request": lambda s: None})()
    d._refuse_pack = None

    assert await _settle(d, d._port, _processing()) == _processing()

"""A WALL OF SWITCHES IS STILL A CONSENT WALL.

MEASURED SHAPE, generalised.  vkpower's signature page renders its six
consents as native ``<input type=checkbox>`` (inventory kind: ``checkbox``)
and the wall experiment was written against exactly that.  A component-library
application renders the SAME wall as ``<button role="checkbox">`` (kind:
``checkbox``) or ``<button role="switch">`` (kind: ``toggle``) — identical to
the user, driven by the identical ``set_checked`` primitive, and invisible to
an experiment that asked for one kind by name.

These tests run the real ``_consent_wall_to_unblock`` over a switch-built wall
and hold the licence unchanged: the operator's NAMED grant for the gated
commit, all-or-nothing checking, and the application's own re-render as the
only verdict.  The falsification control removes the grant and requires the
wall to be left untouched — a wall that clears without a licence is the
form-filling spree the narrow licence exists to prevent.
"""
from __future__ import annotations

import asyncio

from app.walker import WalkerMixin

_URL = "http://x/apply/signature/"
_COMMIT = "Sign & Submit Application"
_CONSENTS = ("HIPAA release", "MIB authorization", "Fraud notice",
             "E-signature consent")


def _wall(*, cleared: bool):
    """The signature step: switches + the gated commit, before/after."""
    disabled = not cleared
    return [
        *[{"kind": "toggle", "role": "switch", "name": n,
           "value_committed": "true" if cleared else ""}
          for n in _CONSENTS],
        {"kind": "text", "name": "Type your full legal name"},
        {"kind": "button", "name": _COMMIT, "disabled": disabled},
    ]


class _Port:
    """set_checked-capable port whose page enables the commit only once every
    switch is on — the application's own gate, scripted."""

    def __init__(self):
        self.checked: list[tuple[str, bool]] = []
        self._on: set[str] = set()

    async def current_url(self):
        return _URL

    async def set_checked(self, control, value):
        name = str(control.get("name") or "")
        self.checked.append((name, bool(value)))
        (self._on.add if value else self._on.discard)(name)

        class _Obs:
            intent_met = True
            committed_value = "true" if value else "false"
        return _Obs()

    def controls(self):
        return _wall(cleared=set(_CONSENTS) <= self._on)


class _Obs:
    def __init__(self, raw):
        self.raw_controls = raw
        self.url = _URL


class _Fill:
    def __init__(self):
        self.unfilled_fields = list(_CONSENTS)
        self.field_ledger = [
            {"name": n, "provenance": "needs_input", "filled": False}
            for n in _CONSENTS]


class _W(WalkerMixin):
    def __init__(self, *, granted: bool):
        self._port = _Port()
        self._refuse_pack = None
        self._submit_approvals = {_COMMIT.lower()} if granted else set()
        self._boundary_grants = None
        self._advance_blocked = [{"url": _URL[:300], "label": _COMMIT[:120]}]
        self._fields_unfilled = list(_CONSENTS)
        self._fields_seed_detail = [{"label": n, "url": _URL} for n in _CONSENTS]
        self._field_ledger = [
            {"name": n, "provenance": "needs_input", "filled": False}
            for n in _CONSENTS]

    async def _observe(self):
        return _Obs(self._port.controls())


def _run(w, fill):
    return asyncio.run(w._consent_wall_to_unblock(
        _wall(cleared=False), _COMMIT, _URL, fill))


def test_a_switch_wall_clears_under_the_named_grant():
    """The wall the experiment could not see: four switches, the operator's
    own named commit, the application enabling it once all four are on."""
    w = _W(granted=True)
    fill = _Fill()
    refreshed = _run(w, fill)
    assert len(w._port.checked) == 4
    assert all(on for _n, on in w._port.checked), "nothing may be un-checked"
    commit = next(c for c in refreshed if c.get("name") == _COMMIT)
    assert not commit.get("disabled"), (
        "the application's own verdict — the enabled commit — must be what "
        "comes back")
    assert fill.unfilled_fields == [], (
        "answered consents must leave the residue the client is asked for")
    assert all(r["provenance"] == "answered_to_unblock"
               for r in w._field_ledger), (
        "the ledger must record the app-confirmed answers")


def test_control_without_the_grant_the_wall_is_untouched():
    """FALSIFICATION CONTROL: identical wall, identical switches — only the
    operator's named grant is gone.  Not one switch may move."""
    w = _W(granted=False)
    refreshed = _run(w, _Fill())
    assert w._port.checked == []
    assert [c.get("name") for c in refreshed] == [
        c.get("name") for c in _wall(cleared=False)]


def test_control_a_wall_the_app_does_not_clear_is_reverted():
    """The experiment's own honesty: check every switch, and when the commit
    stays disabled the theory was wrong — every switch is put back and the
    block stands, named."""
    w = _W(granted=True)

    class _StubbornPort(_Port):
        def controls(self):
            return _wall(cleared=False)      # the app never enables it

    w._port = _StubbornPort()
    _run(w, _Fill())
    ons = [n for n, on in w._port.checked if on]
    offs = [n for n, on in w._port.checked if not on]
    assert sorted(ons) == sorted(offs) == sorted(_CONSENTS), (
        "every switch set must be reverted when the app declines the theory")

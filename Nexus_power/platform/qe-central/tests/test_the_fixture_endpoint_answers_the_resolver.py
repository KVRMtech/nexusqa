"""THE ENVIRONMENT PROVIDER, BOTH ENDS (I1).

``RestProvider`` has always been tested against a mock written by the same hand
as the provider. That proves the two agree with each other; it cannot prove the
contract is implementable, because there was nothing in this repository on the
other end of it.

These tests run the REAL fixture service
(``proving-grounds/fixture-endpoint/server.py``) on a real socket and point the
REAL provider at it. Every assertion below is a behaviour the provider actually
depends on, so a fixture that drifted from the contract fails here rather than
in a client's first crawl.
"""
from __future__ import annotations

import importlib.util
import pathlib
import threading

import pytest

from app.services.env_data_transports import RestProvider

_FIXTURE = (pathlib.Path(__file__).resolve().parents[3]
            / "proving-grounds" / "fixture-endpoint" / "server.py")


def _load_fixture_module():
    spec = importlib.util.spec_from_file_location("qec_fixture_endpoint", _FIXTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)                       # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def fixture_module():
    assert _FIXTURE.exists(), (
        f"the fixture endpoint is missing at {_FIXTURE} — every test below "
        "would then be skipped rather than failing, which is how a missing "
        "proving ground stays missing")
    return _load_fixture_module()


def _serve(module, token: str = ""):
    """Start the fixture on an ephemeral port; return (base_url, shutdown)."""
    server = module.build_server(port=0, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}", server.shutdown


@pytest.fixture()
def open_endpoint(fixture_module):
    base, stop = _serve(fixture_module)
    yield base
    stop()


# ── the contract, exercised end to end ──────────────────────────────────────

def test_the_provider_reads_the_slot_list(open_endpoint, fixture_module):
    slots = RestProvider(base_url=open_endpoint).slots()
    assert "member number" in slots
    assert sorted(slots) == sorted(fixture_module.SLOTS)


def test_the_provider_reads_a_value(open_endpoint):
    assert RestProvider(base_url=open_endpoint).value("member number") == "25000001"


def test_a_slot_key_with_a_space_survives_the_url(open_endpoint):
    """The provider quotes the key; a fixture that did not unquote it would
    answer 404 for every multi-word slot — which is most of them."""
    assert RestProvider(base_url=open_endpoint).value("date of birth") == "1970-04-12"


def test_an_unknown_slot_declines_rather_than_answering_none(open_endpoint):
    """A 200 carrying {"value": null} would hand the resolver a None it had
    successfully fetched, and an unanswerable slot would read as answered. The
    fixture returns 404 and the provider declines."""
    assert RestProvider(base_url=open_endpoint).value("shoe size") is None


# ── auth is enforced, not decorative ────────────────────────────────────────

def test_a_configured_token_is_required(fixture_module):
    base, stop = _serve(fixture_module, token="s3cret")
    try:
        assert RestProvider(base_url=base, token="s3cret").value("member number") \
            == "25000001"
        # THE CONTROL. Without this the test above passes against a fixture that
        # ignores Authorization entirely, and a broken auth path ships green.
        assert RestProvider(base_url=base, token="wrong").value("member number") is None
        assert RestProvider(base_url=base).slots() == []
    finally:
        stop()


# ── falsification controls ──────────────────────────────────────────────────

def test_a_dead_endpoint_declines_and_does_not_raise(fixture_module):
    """The client's environment is not ours to keep alive. Proven by pointing
    the provider at a port nothing is listening on."""
    base, stop = _serve(fixture_module)
    stop()                                   # kill it, then ask
    provider = RestProvider(base_url=base, timeout_s=0.25)
    assert provider.slots() == []
    assert provider.value("member number") is None


def test_the_fixture_is_actually_being_reached(open_endpoint):
    """Every assertion above would also pass against a provider that declined
    everything. This one fails if nothing is really answering."""
    assert RestProvider(base_url=open_endpoint).slots(), (
        "the provider read an empty slot list — the fixture is not answering, "
        "and every decline-shaped assertion above is vacuous")


def test_no_served_value_exceeds_the_providers_cap(fixture_module):
    """MAX_VALUE_CHARS is the provider's rule; a fixture that served more would
    be teaching clients a shape the resolver rejects."""
    too_long = [k for k, v in fixture_module.SLOTS.items()
                if len(v) > fixture_module.MAX_VALUE_CHARS]
    assert not too_long, too_long

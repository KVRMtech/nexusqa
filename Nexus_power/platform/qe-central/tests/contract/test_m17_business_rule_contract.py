"""M1.7 / T-GW-04 — THE QE-CENTRAL HALF of the cross-service rule contract.

Read the header of ``engines/qe-explorer/tests/test_m17_wire_contract.py`` first:
it explains why this proof is split across two files.  In short — qe-explorer and
qe-central each ship a top-level ``app`` package and cannot be imported into one
interpreter, so the shape they must agree on is frozen as DATA in
``contracts/m17_business_rule_v1.json`` and each side asserts against it in its
own process.  Rename a field on either side and that side's suite fails.

WHAT WAS ACTUALLY UNPROVEN BEFORE THIS FILE.  ``test_greenwash_recovery.py``
proves this store validates and bounds a rule; it builds its inputs from
hand-written literals.  So it proves the store is internally consistent with
ITSELF, and would keep passing if the explorer had stopped sending the names it
reads.  The failure mode is silent: an unrecognised rule is dropped, the crawl
falls back to re-running the experiment, and the result is still CORRECT — just
never learned.  T-GW-04's acceptance ("subsequent crawls reuse persisted
knowledge") would report success over a severed loop.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.models import QEBusinessRuleRow
from app.services import rule_store


def _contract() -> dict:
    """Load the frozen contract by walking up to the ``Nexus_power`` root.

    Walked rather than hard-coded: this suite is collected from the repository
    root in CI (``pytest platform/qe-central/tests``) and from the service root
    locally, and a relative literal would resolve in only one of them.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "m17_business_rule_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/m17_business_rule_v1.json not found above %s — the frozen "
        "wire contract is the only thing tying this store's rule shape to the "
        "explorer's, and it must not be deleted to make a test pass" % here
    )


CONTRACT = _contract()


def test_the_store_accepts_a_rule_in_the_contract_shape():
    """T-GW-04. The explorer emits exactly this on completion; if the store drops
    it, every proved rule is discarded at the door and nothing is ever learned."""
    cleaned = rule_store._clean(CONTRACT["sample"])

    assert cleaned is not None, (
        "rule_store._clean() refused a rule in the frozen wire shape — the "
        "explorer's DiscoveredRule.as_dict() emits exactly this"
    )
    # ``key`` is the only rename across the boundary, and it is deliberate: the
    # wire calls it ``key``, the column calls it ``rule_key``.  Assert the
    # mapping explicitly so the rename stays a decision rather than a drift.
    assert cleaned["rule_key"] == CONTRACT["sample"]["key"]
    for name in CONTRACT["required_fields"]:
        if name == "key":
            continue
        assert cleaned[name] == CONTRACT["sample"][name], (
            "field %r did not survive _clean()" % name
        )


def test_every_contract_field_has_a_column_to_land_in():
    """T-GW-04. A validated field with nowhere to be stored is a field that is
    silently dropped at the INSERT rather than at the door — the same severed
    loop, one layer deeper and harder to see."""
    columns = set(QEBusinessRuleRow.__table__.columns.keys())
    for name in CONTRACT["required_fields"]:
        column = "rule_key" if name == "key" else name
        assert column in columns, (
            "contract field %r has no column on qe_business_rules" % name
        )


def test_column_widths_match_the_bounds_the_explorer_truncates_to():
    """T-GW-04. Both sides bound the same strings to the same widths.

    If a column were NARROWER than the explorer's truncation, a long but legal
    label would be rejected by Postgres and the rule lost; if WIDER, the two
    sides would disagree about what the stored rule is.  Equality keeps the
    truncation a single decision made once.
    """
    columns = QEBusinessRuleRow.__table__.columns
    for name, expected in CONTRACT["column_bounds"].items():
        column = columns["rule_key" if name == "key" else name]
        assert column.type.length == expected, (
            "%s is String(%s) but the contract bounds it to %d"
            % (name, column.type.length, expected)
        )


@pytest.mark.parametrize("missing", ["key", "blocked_label", "field_label"])
def test_a_fragment_is_refused_on_this_side_too(missing):
    """T-GW-04. Both sides refuse the identical set of fragments.

    A rule one side stores and the other ignores is worse than a rule neither
    stores: the store reports a row written and the reuse metric counts a rule
    known, while the crawl re-runs the experiment every time.
    """
    assert missing in CONTRACT["must_be_present_or_the_rule_is_a_fragment"]
    fragment = dict(CONTRACT["sample"])
    fragment[missing] = ""

    assert rule_store._clean(fragment) is None


@pytest.mark.asyncio
async def test_the_dispatch_hands_back_exactly_the_contract_fields(monkeypatch):
    """T-GW-04 ACCEPTANCE, qe-central side. ``fetch_rules`` is a pass-through.

    The row -> wire projection is the last place the two vocabularies can drift,
    and it is the one the explorer's ``from_mapping`` parses.  Exercised against
    a stubbed session so the CONTRACT is proven on every push rather than only in
    the Postgres-backed lane — the shape is not a database property.
    """
    row = SimpleNamespace(
        rule_key=CONTRACT["sample"]["key"],
        kind=CONTRACT["sample"]["kind"],
        url_template=CONTRACT["sample"]["url_template"],
        blocked_label=CONTRACT["sample"]["blocked_label"],
        field_label=CONTRACT["sample"]["field_label"],
        proof=CONTRACT["sample"]["proof"],
        schema_version=CONTRACT["sample"]["schema_version"],
    )

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: [row])

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(rule_store, "tenant_scoped_qec_session", lambda _t: _Ctx())

    fetched = await rule_store.fetch_rules("t1", "a1")

    assert len(fetched) == 1
    assert set(fetched[0]) == set(CONTRACT["required_fields"]), (
        "fetch_rules() no longer emits the frozen wire shape — the explorer's "
        "DiscoveredRule.from_mapping() would fail closed on it and silently fall "
        "back to re-running every experiment"
    )
    assert fetched[0] == CONTRACT["sample"]


def test_the_supported_version_is_the_contract_version():
    """T-GW-04. The store must not claim to support a version the frozen contract
    has not defined — that is how a shape change ships without a reader."""
    assert rule_store.SUPPORTED_SCHEMA_VERSION == CONTRACT["schema_version"]

"""Auth-capture -> login recipe: the RECORD-ONCE bridge, and its fail-safe.

When an operator logs in by hand in the capture browser we store two things: the
encrypted session (that one member) and a replayable recipe (every member). This
pins the contract between them — above all that recipe derivation is strictly
SUBORDINATE: it may fail for any reason, but it must never raise, and must never
cost the operator the session they just captured.
"""
import pytest

from app.routers.test_factory import _derive_recipe_from_capture
from app.services.test_factory import persona_store


class _FakeSession:
    """Minimal async session — records what save_recipe would have added."""

    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def execute(self, *_a, **_kw):
        return _EmptyResult()

    async def flush(self):
        return None


def test_recipe_source_fits_the_column():
    """tp_login_recipes.source is String(24); an overflow would DataError inside
    the best-effort wrapper and silently produce zero recipes, forever."""
    from app.services.test_factory.persona_store import TpLoginRecipeRow
    assert len("login_recording") <= TpLoginRecipeRow.__table__.c.source.type.length


def test_domain_reduction_agrees_with_the_crawler():
    """Both reducers feed the SAME login_type_key. If they ever disagree, fleet
    reuse silently never matches — find_recipes_by_login_type just returns []."""
    from app.services.test_factory import login_observation as lo
    for host in ("www.usaa.com", "auth.example.co.uk", "portal.aig.com",
                 "a.b.c.example.com.au", "localhost", "127.0.0.1",
                 "example.com", "sub.example.org", "x.co.jp", "deep.sub.gov.in"):
        expected = lo.registrable_domain(host)
        # recompute independently with the same documented rule
        h = host.strip().lower().rstrip(".")
        labels = h.split(".")
        if lo._looks_like_ip(h) or "." not in h or len(labels) <= 2:
            manual = h
        elif ".".join(labels[-2:]) in lo._MULTI_PART_SUFFIXES:
            manual = ".".join(labels[-3:])
        else:
            manual = ".".join(labels[-2:])
        assert expected == manual, host


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


def _snapshot(events, start="https://www.usaa.com/login", current="", truncated=False):
    return {"observer_version": "v1", "start_url": start,
            "current_url": current or start, "truncated": truncated, "events": events}


def _fill(i, name, type_="text"):
    return {"kind": "fill", "sequence_index": i, "name": name, "id": "",
            "label": name, "type": type_, "autocomplete": "", "filled": True}


def _click(i, text):
    return {"kind": "click", "sequence_index": i, "text": text, "role": "button",
            "name": "", "id": "", "type": "submit"}


@pytest.mark.asyncio
async def test_records_a_recipe_from_an_observed_login():
    session = _FakeSession()
    info = await _derive_recipe_from_capture(
        session, tenant_id="t1", artifact_id="a1",
        snapshot=_snapshot([
            _fill(0, "member_number"), _fill(1, "password", "password"),
            _fill(2, "pin"), _click(3, "Sign in"),
            {"kind": "navigate", "sequence_index": 4,
             "url": "https://www.usaa.com/portal/home"},
        ], current="https://www.usaa.com/portal/home"))

    assert info["recorded"] is True
    assert info["slots"] == ["member_number", "password", "pin"]
    assert info["login_type_key"].startswith("lt_")
    assert info["version"] == 1
    assert len(session.added) == 1          # exactly one recipe row persisted


@pytest.mark.asyncio
async def test_aig_email_login_records_the_same_way():
    """Genericity at the bridge, not just in the pure core."""
    session = _FakeSession()
    info = await _derive_recipe_from_capture(
        session, tenant_id="t1", artifact_id="a1",
        snapshot=_snapshot([
            _fill(0, "email"), _fill(1, "password", "password"),
            _fill(2, "pin"), _click(3, "Continue"),
        ], start="https://www.aig.com/login"))
    assert info["recorded"] is True
    assert info["slots"] == ["email", "password", "pin"]


# ── the fail-safe: never lose the session ───────────────────────────────────

@pytest.mark.asyncio
async def test_no_observation_is_reported_not_raised():
    info = await _derive_recipe_from_capture(
        _FakeSession(), tenant_id="t1", artifact_id="a1", snapshot=None)
    assert info == {"recorded": False, "reason": "no_observation_from_runner"}


@pytest.mark.asyncio
async def test_login_with_no_fields_is_reported_not_raised():
    info = await _derive_recipe_from_capture(
        _FakeSession(), tenant_id="t1", artifact_id="a1",
        snapshot=_snapshot([{"kind": "navigate", "sequence_index": 0,
                             "url": "https://x.com/login"}]))
    assert info["recorded"] is False
    assert info["reason"] == "no_credential_fields_observed"


@pytest.mark.asyncio
async def test_a_persistence_failure_is_swallowed_so_the_session_survives(monkeypatch):
    """The whole point: if save_recipe blows up, /auth/save must still succeed."""
    async def _boom(*_a, **_kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(persona_store, "save_recipe", _boom)
    info = await _derive_recipe_from_capture(
        _FakeSession(), tenant_id="t1", artifact_id="a1",
        snapshot=_snapshot([_fill(0, "member_number"),
                            _fill(1, "password", "password"), _click(2, "Sign in")]))
    assert info == {"recorded": False, "reason": "derivation_failed"}


@pytest.mark.asyncio
async def test_a_malformed_snapshot_is_swallowed():
    info = await _derive_recipe_from_capture(
        _FakeSession(), tenant_id="t1", artifact_id="a1",
        snapshot={"events": "not-a-list"})
    assert info["recorded"] is False
    assert info["reason"] in ("no_credential_fields_observed", "derivation_failed")


@pytest.mark.asyncio
async def test_truncated_observation_is_surfaced_to_the_operator():
    session = _FakeSession()
    info = await _derive_recipe_from_capture(
        session, tenant_id="t1", artifact_id="a1",
        snapshot=_snapshot([_fill(0, "member_number"),
                            _fill(1, "password", "password"), _click(2, "Sign in")],
                           truncated=True))
    assert info["recorded"] is True
    assert info["truncated"] is True     # a long session may have dropped events


@pytest.mark.asyncio
async def test_recapturing_an_unchanged_login_does_not_mint_a_version(monkeypatch):
    """Re-capturing an expired session must NOT thrash the recipe: a new version
    supersedes the active row and clears the verified_at certification reads."""
    events = [_fill(0, "member_number"), _fill(1, "password", "password"),
              _click(2, "Sign in")]
    session = _FakeSession()
    first = await _derive_recipe_from_capture(
        session, tenant_id="t1", artifact_id="a1", snapshot=_snapshot(list(events)))
    assert first["recorded"] is True

    # second capture of the SAME login — get_recipe now returns what we stored
    stored_steps = session.added[0].steps

    async def _existing(*_a, **_kw):
        return {"recipe_id": "r1", "version": 1, "steps": stored_steps}

    monkeypatch.setattr(persona_store, "get_recipe", _existing)
    session2 = _FakeSession()
    second = await _derive_recipe_from_capture(
        session2, tenant_id="t1", artifact_id="a1", snapshot=_snapshot(list(events)))

    assert second["recorded"] is False
    assert second["reason"] == "unchanged"
    assert second["version"] == 1
    assert session2.added == []          # nothing persisted


@pytest.mark.asyncio
async def test_a_CHANGED_login_does_mint_a_new_version(monkeypatch):
    """The flip side: if the app's login actually changed, record it."""
    async def _existing(*_a, **_kw):
        return {"recipe_id": "r1", "version": 1,
                "steps": [{"action": "goto", "path": "/login"}]}   # different

    monkeypatch.setattr(persona_store, "get_recipe", _existing)
    session = _FakeSession()
    info = await _derive_recipe_from_capture(
        session, tenant_id="t1", artifact_id="a1",
        snapshot=_snapshot([_fill(0, "member_number"),
                            _fill(1, "password", "password"), _click(2, "Sign in")]))
    assert info["recorded"] is True
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_no_credential_value_is_ever_persisted():
    session = _FakeSession()
    secret = "50000005"
    events = [dict(_fill(0, "member_number"), value=secret),
              _fill(1, "password", "password"), _click(2, "Sign in")]
    await _derive_recipe_from_capture(
        session, tenant_id="t1", artifact_id="a1", snapshot=_snapshot(events))
    row = session.added[0]
    assert secret not in repr(row.steps)
    assert secret not in repr(row.slots)

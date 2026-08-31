"""RUNG 2 REACHES DISPATCH: THE ENVIRONMENT ANSWERS WHAT THE CATALOG ASKS.

THE DECISION THIS FILE PINS. At dispatch time the crawl has not run, so field
labels are not knowable — except that every crawl after the first has the
previous crawls' CATALOG, which is precisely the list of questions this
application asks. Resolving the environment against those labels keeps the
per-label ambiguity refusal intact; pushing raw slots into the semantic map
would hand a two-slot collision to a substring matcher — the coin toss the rung
exists to refuse.

The corollary is stated rather than hidden: A FIRST CRAWL RUNS WITHOUT
ENVIRONMENT ANSWERS. Its residue teaches the catalog, and the second crawl asks
the environment. That is honest sequencing, not a gap.
"""
from __future__ import annotations

import pytest

from app.routers.explorations import _environment_labels


class _Row:
    def __init__(self, answer_key):
        self.answer_key = answer_key


def _catalog(*labels):
    return {"questions": [{"name": label, "text": ""} for label in labels]}


# ── the gate: ordinary apps pay nothing ────────────────────────────────────

@pytest.mark.asyncio
async def test_an_app_with_no_environment_reads_no_catalog_at_all(monkeypatch):
    """THE GATE THAT KEEPS EVERY ORDINARY DISPATCH FREE. An app that declared
    no environment must not even pay a catalog read — asserted by making the
    read explode."""
    from app.services import catalog_store

    async def _boom(*a, **k):
        raise AssertionError("the catalog must not be read for this app")

    monkeypatch.setattr(catalog_store, "build_app_master_catalog", _boom)
    assert await _environment_labels("t1", "a1", _Row({})) == []
    assert await _environment_labels("t1", "a1", _Row(None)) == []
    assert await _environment_labels(
        "t1", "a1", _Row({"fill": {"x": "y"}})) == []


# ── the labels are the catalog's questions ─────────────────────────────────

@pytest.mark.asyncio
async def test_the_catalog_s_questions_become_the_resolution_keys(monkeypatch):
    from app.services import catalog_store

    async def _catalog_for(tenant_id, app_id):
        assert (tenant_id, app_id) == ("t1", "a1")
        return _catalog("Member ID", "Policy Number")

    monkeypatch.setattr(catalog_store, "build_app_master_catalog", _catalog_for)
    row = _Row({"environment": {"kind": "manifest",
                                "values": {"member id": "M-1"}}})
    assert await _environment_labels("t1", "a1", row) == [
        "Member ID", "Policy Number"]


@pytest.mark.asyncio
async def test_a_first_crawl_with_no_catalog_yet_is_honestly_inert(monkeypatch):
    """The stated corollary: no catalog, no environment answers, no error."""
    from app.services import catalog_store

    async def _empty(tenant_id, app_id):
        return {"questions": []}

    monkeypatch.setattr(catalog_store, "build_app_master_catalog", _empty)
    row = _Row({"environment": {"kind": "manifest", "values": {"a": "1"}}})
    assert await _environment_labels("t1", "a1", row) == []


@pytest.mark.asyncio
async def test_a_catalog_that_cannot_be_read_declines_rather_than_500s(monkeypatch):
    from app.services import catalog_store

    async def _down(*a, **k):
        raise RuntimeError("db restarting")

    monkeypatch.setattr(catalog_store, "build_app_master_catalog", _down)
    row = _Row({"environment": {"kind": "manifest", "values": {"a": "1"}}})
    assert await _environment_labels("t1", "a1", row) == []


@pytest.mark.asyncio
async def test_duplicate_questions_resolve_once_and_the_list_is_bounded(monkeypatch):
    from app.services import catalog_store

    async def _many(tenant_id, app_id):
        qs = [{"name": "Member ID"}] * 3
        qs += [{"name": f"Q{i}"} for i in range(600)]
        return {"questions": qs}

    monkeypatch.setattr(catalog_store, "build_app_master_catalog", _many)
    row = _Row({"environment": {"kind": "manifest", "values": {"a": "1"}}})
    labels = await _environment_labels("t1", "a1", row)
    assert labels.count("Member ID") == 1
    assert len(labels) == 500


# ── the wiring order, pinned in source ─────────────────────────────────────

def test_the_overlay_is_applied_after_credentials_and_before_the_payload():
    """The three lines whose ORDER is the feature: the projection first, the
    overlay re-projection after the credential decrypt (the token lives in the
    envelope), and the dispatch payload last. A refactor that reorders them
    silently disables the rung or reads a token that is not there yet."""
    import inspect

    from app.routers import explorations

    source = inspect.getsource(explorations)
    first = source.index("answer_key = explorer_fill_contract(row.answer_key)")
    decrypt = source.index("credentials = await _decrypt_credentials(")
    overlay = source.index(
        "answer_key = explorer_fill_contract(row.answer_key, env_overlay)")
    payload = source.index("answer_key=answer_key,")
    assert first < decrypt < overlay < payload


def test_the_token_is_read_from_the_decrypted_envelope_not_the_answer_key():
    import inspect

    from app.routers import explorations

    source = inspect.getsource(explorations)
    assert 'token=str((credentials or {}).get("env_token") or "")' in source

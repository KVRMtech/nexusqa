"""T-FE-04 / T-FE-09 — THE KEY THAT MADE LEARNING EXPIRE AFTER ONE CRAWL.

One mistake, two symptoms.  ``tp_field_memory`` was keyed
``(tenant, artifact, signature)`` and the identity seed came back as
``tenant::artifact`` — and a re-crawl MINTS A NEW ARTIFACT.  So crawl N stored
its answers under artifact N, crawl N+1 read artifact N and wrote to N+1, and
crawl N+2 could see nothing crawl N had learned; meanwhile the synthetic
applicant changed on every run, which makes a rate quote move for a reason
nobody can distinguish from a regression afterwards.

A scope is the fix, and these tests pin the properties that make one safe.
"""
from __future__ import annotations

import pytest

from app.fill_engine.learning import (MEMORY_SCOPE_VERSION, identity_seed,
                                      memory_scope, parse_scope, scope_digest)
from app.fill_engine.persona import derive_persona


def test_a_scope_is_stable_for_one_tenant_and_application():
    """The whole point: it contains nothing that changes between runs."""
    assert memory_scope("acme", "app-7") == memory_scope("acme", "app-7")
    assert "artifact" not in memory_scope("acme", "app-7")


def test_two_applications_of_one_tenant_are_isolated():
    assert memory_scope("acme", "app-7") != memory_scope("acme", "app-8")


def test_two_tenants_are_isolated():
    assert memory_scope("acme", "app-7") != memory_scope("globex", "app-7")


def test_a_separator_in_an_id_cannot_forge_another_tenants_scope():
    """A cross-tenant read is the one failure here that is not recoverable, so
    the separator is escaped rather than trusted."""
    forged = memory_scope("acme::app-7", "x")
    honest = memory_scope("acme", "app-7")
    assert forged != honest
    assert parse_scope(forged) == (MEMORY_SCOPE_VERSION, "acme::app-7", "x")


def test_a_scope_carries_its_version_so_old_rows_are_identifiable():
    assert memory_scope("acme", "app-7").startswith(f"v{MEMORY_SCOPE_VERSION}::")
    assert parse_scope(memory_scope("acme", "a"))[0] == MEMORY_SCOPE_VERSION


def test_an_artifact_id_cannot_be_passed_off_as_an_application():
    """A caller holding only a per-run handle must resolve the application
    first.  Accepting it silently is how the defect got in."""
    with pytest.raises(ValueError, match="artifact"):
        memory_scope("acme", "")
    with pytest.raises(ValueError, match="tenant"):
        memory_scope("", "app-7")


def test_the_identity_seed_and_the_memory_share_one_scope():
    """A person who rotates while their remembered values persist produces a
    form holding one person's postcode beside another person's name."""
    assert identity_seed("acme", "app-7") == memory_scope("acme", "app-7")


def test_two_crawls_of_one_application_derive_the_identical_applicant():
    """Deterministic replay, stated as the acceptance criterion does."""
    first = derive_persona(identity_seed("acme", "summit-life"))
    second = derive_persona(identity_seed("acme", "summit-life"))
    assert first.as_dict() == second.as_dict()


def test_a_crawl_of_a_different_application_derives_a_different_applicant():
    a = derive_persona(identity_seed("acme", "summit-life"))
    b = derive_persona(identity_seed("acme", "summit-health"))
    assert a.applicant.full_name != b.applicant.full_name


def test_the_old_artifact_keyed_seed_would_have_rotated_the_person():
    """The defect, demonstrated rather than described: the seed the platform
    used to return changed every crawl, so the applicant did too."""
    people = {derive_persona(f"acme::artifact-{n}").applicant.full_name
              for n in range(6)}
    assert len(people) > 1, "the artifact-keyed seed produced a stable person?"
    stable = {derive_persona(identity_seed("acme", "one-app")).applicant.full_name
              for _ in range(6)}
    assert len(stable) == 1


def test_a_digest_identifies_a_scope_in_a_log_without_naming_the_tenant():
    scope = memory_scope("acme", "app-7")
    digest = scope_digest(scope)
    assert digest == scope_digest(scope) and len(digest) == 16
    assert "acme" not in digest

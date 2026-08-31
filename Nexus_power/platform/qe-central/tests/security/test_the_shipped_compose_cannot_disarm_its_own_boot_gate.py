"""R1 · THE SHIPPED COMPOSE MUST NOT DISARM THE BOOT GATE, NOR SHIP A SECRET.

MEASURED ON THE LIVE VM, 2026-08-31, which is why this test exists rather than
a paragraph:

    nexus-qe-central   NEXUS_ENV = production     <- ${NEXUS_ENV:-development}
    nexus-qe-explorer  NEXUS_ENV = production
    nexus-platform-api NEXUS_ENV = development    <- a LITERAL in its service

``.env.production`` said ``NEXUS_ENV=production`` and the deploy passed it with
``--env-file``. It changed nothing for platform-api, because a literal in a
service's ``environment:`` block beats an env file. ``validate_boot_safety``
only refuses in ``{staging, production}``, so on the one host that serves
clients that service's fail-closed gate was inert — the exact shape of finding
R1, still true two weeks after it was written.

TWO RULES, BOTH READ OFF THE FILES THEMSELVES:

  1. ``NEXUS_ENV`` is never a literal. It must be ``${NEXUS_ENV...}`` so the
     deploy environment decides, and a deployed process cannot be told it is a
     development one.
  2. A SECRET HAS NO SHIPPED DEFAULT. ``${X:-something}`` on a secret is not a
     default, it is a published credential: it lands in every checkout, every
     image layer and every screenshot of the file. Secrets must use
     ``${X:?message}`` so a misconfigured deploy fails loudly instead of
     authenticating with a value the whole internet can read.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

#: repo/Nexus_power — the composes the deploy actually uses.
_ROOT = Path(__file__).resolve().parents[4]
_COMPOSES = sorted(p for p in _ROOT.glob("docker-compose*.yml") if p.is_file())

#: Keys whose VALUE is a credential. Matched on the key name, so a new secret
#: added later is covered without editing this list.
_SECRET_RE = re.compile(
    r"^\s*([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*)\s*:\s*(.+?)\s*$")

#: Keys whose NAME contains a secret word but whose VALUE is not a credential —
#: a lifetime, an expiry, a count. Matched exactly, so a real secret can never
#: hide behind a suffix.
_NOT_SECRETS = frozenset({
    "QEC_SERVICE_TOKEN_TTL_SECONDS",
    "QEC_EXPLORER_TOKEN_PREVIOUS_EXPIRES_AT",
})

#: THE KNOWN INFRASTRUCTURE DEFAULTS, RECORDED RATHER THAN SILENTLY FIXED.
#:
#: These are real instances of the same class as R1 — a credential with a
#: shipped default — and they are NOT fixed here. Making them required would
#: change the database password that local development and CI's compose-based
#: lanes boot with, and this test cannot verify that those still start. They
#: are frozen instead: each is named, and any NEW secret default fails the
#: build. A register that grows silently is how a finding becomes furniture.
_KNOWN_INFRA_DEFAULTS = frozenset({
    ("POSTGRES_PASSWORD", "nexus-dev"),
    ("MINIO_ROOT_PASSWORD", "minioadmin"),
    ("MINIO_SECRET_ACCESS_KEY", "minioadmin"),
    ("NEO4J_PASSWORD", "nexus-neo4j-dev"),
})

#: A default that is obviously inert (empty, or a pure placeholder reference)
#: is not a shipped credential.
_INERT = ("", "''", '""')


def test_the_composes_are_actually_found():
    """FALSIFICATION CONTROL. Every assertion below is a loop over these files;
    if the glob broke, all of them would pass over nothing."""
    assert _COMPOSES, f"no docker-compose*.yml found under {_ROOT}"
    names = {p.name for p in _COMPOSES}
    assert "docker-compose.yml" in names
    assert "docker-compose.qec.yml" in names


@pytest.mark.parametrize("compose", _COMPOSES, ids=lambda p: p.name)
def test_nexus_env_is_never_pinned_to_a_literal(compose):
    """RULE 1. A literal here silently outranks --env-file, and the boot gate
    only bites in a deployed env — so a pinned `development` disarms it on the
    host that serves clients."""
    offenders = [
        (i, line.strip())
        for i, line in enumerate(compose.read_text(encoding="utf-8").splitlines(), 1)
        if re.match(r"^\s*NEXUS_ENV\s*:", line) and "${" not in line
    ]
    assert not offenders, (
        f"{compose.name} pins NEXUS_ENV to a literal at "
        f"{[i for i, _ in offenders]} — use ${{NEXUS_ENV:-development}} so the "
        f"deploy environment decides: {offenders[:3]}")


@pytest.mark.parametrize("compose", _COMPOSES, ids=lambda p: p.name)
def test_no_secret_carries_a_shipped_default(compose):
    """RULE 2. `${X:-a-value}` on a secret puts that value in every checkout
    and image layer. Required (`${X:?...}`) turns a misconfigured deploy into a
    loud failure instead of a quiet authentication with a published string."""
    offenders = []
    for i, line in enumerate(compose.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        found = _SECRET_RE.match(line)
        if not found:
            continue
        key, value = found.group(1), found.group(2).strip()
        if key in _NOT_SECRETS:
            continue
        default = re.search(r"\$\{[A-Z0-9_]+:-(.*?)\}$", value)
        if not default:
            continue
        fallback = default.group(1).strip()
        if fallback in _INERT or (key, fallback) in _KNOWN_INFRA_DEFAULTS:
            continue
        offenders.append((i, key, fallback[:40]))
    assert not offenders, (
        f"{compose.name} ships a DEFAULT for a secret — that is a published "
        f"credential, not a default. Use ${{KEY:?message}}: {offenders[:5]}")


def test_the_known_published_default_is_gone_everywhere():
    """The specific string finding R1 named. Kept as its own assertion so the
    record of the defect and its absence travel together."""
    for compose in _COMPOSES:
        # CONFIG lines only. qec.yml's own comments narrate this exact string
        # to explain why the key is required there — reading a comment as a
        # shipped credential would punish the file that documents the fix.
        config = [ln for ln in compose.read_text(encoding="utf-8").splitlines()
                  if not ln.lstrip().startswith("#")]
        offenders = [i for i, ln in enumerate(config, 1)
                     if "test-secret-do-not-use-in-production" in ln]
        assert not offenders, (
            f"{compose.name} still ships the development JWT secret R1 named")


@pytest.mark.parametrize("compose", _COMPOSES, ids=lambda p: p.name)
def test_the_api_port_is_not_published_to_every_interface(compose):
    """R1's third clause. 8093 is the whole qe-central API; published on
    0.0.0.0 it is reachable from anywhere the host is, with no edge auth in
    front of it."""
    offenders = [
        (i, line.strip())
        for i, line in enumerate(compose.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r'^\s*-\s*["\']?(0\.0\.0\.0:)?8093:8093', line)
    ]
    assert not offenders, (
        f"{compose.name} publishes 8093 to all interfaces at "
        f"{[i for i, _ in offenders]} — bind it to "
        f"${{QEC_BIND_ADDRESS:-127.0.0.1}} or put an authenticating edge in front")


def test_the_known_register_still_describes_reality():
    """A FROZEN REGISTER MUST NOT OUTLIVE ITS SUBJECT. Every entry above is a
    claim that a real shipped default exists; if one is fixed the entry becomes
    a licence for a defect that is no longer there, so this fails and tells
    whoever fixed it to delete the line."""
    seen = set()
    for compose in _COMPOSES:
        for line in compose.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            found = _SECRET_RE.match(line)
            if not found:
                continue
            default = re.search(r"\$\{[A-Z0-9_]+:-(.*?)\}$", found.group(2).strip())
            if default:
                seen.add((found.group(1), default.group(1).strip()))
    stale = _KNOWN_INFRA_DEFAULTS - seen
    assert not stale, (
        f"these known defaults are gone — delete them from "
        f"_KNOWN_INFRA_DEFAULTS: {sorted(stale)}")

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

#: The frozen register is intentionally empty.  It exists to make a future
#: exception conspicuous in review, and ``test_the_known_register_still...``
#: below makes a stale exception fail as soon as its compose value is removed.
#: Team F retired the previous postgres/minio/neo4j entries together with the
#: CI compose lane that now supplies its own ephemeral credentials.
_KNOWN_INFRA_DEFAULTS = frozenset({
    # 2026-09-01 · Team F/H. The DEV redis password, and the only entry here.
    #
    # RULE 2b (below) found three shipped credentials the key-name detector
    # could not see. Two were in docker-compose.qec.yml — the stack that serves
    # clients — and are now `${...:?}`. This third is in docker-compose.dev.yml,
    # which another session holds uncommitted in this shared checkout, and a
    # pathspec commit takes the WHOLE file (CLAUDE.md section 1, learned the
    # hard way in d611592/e00ce6b earlier today).
    #
    # Registered rather than silently skipped, which is what this frozenset is
    # for: it makes the exception conspicuous in review, and
    # `test_the_known_register_still_matches_the_composes` fails the moment the
    # value is removed, so a stale entry cannot outlive its subject.
    #
    # TO CLOSE: change line 25 and 27 of docker-compose.dev.yml to
    # `${REDIS_PASSWORD:?...}` and delete this entry in the same commit.
    ("REDIS_PASSWORD", "nexus-redis-dev"),
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


#: A credential default ANYWHERE on a line, not only under a secret-named key.
#:
#: RULE 2 above matches the YAML KEY, which is right for `NEXUS_JWT_SECRET: ...`
#: and blind to the two shapes that actually shipped:
#:
#:     QEC_DATABASE_URL: postgresql+asyncpg://qec:${QEC_DB_PASSWORD:-qec-dev}@...
#:     command: redis-server --requirepass ${REDIS_PASSWORD:-nexus-redis-dev}
#:
#: The keys are `QEC_DATABASE_URL` and `command` — no secret word in either — so
#: the detector never looked, and the register could be emptied while three
#: published credentials stayed in every checkout and image layer. A password
#: inside a DSN is a password; where it sits in the YAML is not the point.
_EMBEDDED_SECRET_RE = re.compile(
    r"\$\{([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*):-([^}]*)\}")


@pytest.mark.parametrize("compose", _COMPOSES, ids=lambda p: p.name)
def test_no_secret_carries_a_shipped_default_anywhere_on_the_line(compose):
    """RULE 2b. The same rule, matched on the VARIABLE rather than on the key."""
    offenders = []
    for i, line in enumerate(compose.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for key, fallback in _EMBEDDED_SECRET_RE.findall(line):
            fallback = fallback.strip()
            if key in _NOT_SECRETS or fallback in _INERT:
                continue
            if (key, fallback) in _KNOWN_INFRA_DEFAULTS:
                continue
            offenders.append((i, key, fallback[:40]))
    assert not offenders, (
        f"{compose.name} ships a credential DEFAULT inside a value — a password "
        f"in a DSN is still a published password. Use ${{KEY:?message}} and let "
        f"the deploy supply it: {offenders[:5]}")


def test_the_embedded_detector_can_actually_fire():
    """FALSIFICATION CONTROL for the rule above.

    Without this, an expression that matched nothing would pass every compose in
    the repository and read exactly like a clean result — which is the whole
    reason the shipped credentials survived a green security suite."""
    caught = _EMBEDDED_SECRET_RE.findall(
        "  QEC_DATABASE_URL: postgresql://qec:${QEC_DB_PASSWORD:-qec-dev}@db:5432/x")
    assert caught == [("QEC_DB_PASSWORD", "qec-dev")], caught
    assert _EMBEDDED_SECRET_RE.findall(
        "  command: redis-server --requirepass ${REDIS_PASSWORD:-nexus-redis-dev}")
    # …and must NOT fire on a required variable, which is the fixed shape.
    assert not _EMBEDDED_SECRET_RE.findall(
        "  QEC_DATABASE_URL: postgresql://qec:${QEC_DB_PASSWORD:?set it}@db:5432/x")


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
    # …AND WHATEVER RULE 2b SEES, or this guard is blind exactly where the
    # detector was. Scanning only by key name, it could not see a password
    # embedded in a DSN or a `command:` line — so an entry registered for one of
    # those reads as "gone" and the guard demands its deletion, which would
    # delete the record of a credential that is still shipped. The register and
    # the detector must look at the same thing.
    for compose in _COMPOSES:
        for line in compose.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for key, fallback in _EMBEDDED_SECRET_RE.findall(line):
                seen.add((key, fallback.strip()))
    stale = _KNOWN_INFRA_DEFAULTS - seen
    assert not stale, (
        f"these known defaults are gone — delete them from "
        f"_KNOWN_INFRA_DEFAULTS: {sorted(stale)}")

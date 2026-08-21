"""A27.1 — the extensible NO-SILENT-SKIP gate for required infrastructure.

THE PATTERN THIS EXISTS TO KILL::

    infrastructure missing  ->  tests skipped  ->  CI green

A skip is the correct answer on a laptop with no Postgres and no MinIO. It is the
WRONG answer in CI, where the whole point of the job is that those guarantees
hold. So every infrastructure dependency is two-state:

  * the ``QEC_REQUIRE_*`` flag unset (laptop) — a missing endpoint SKIPS, with
    the environment variable named in the reason so the developer knows what to
    set;
  * the flag set (CI) — a skip for want of that infrastructure FAILS the whole
    session, listed by name.

WHY THIS IS A REGISTRY AND NOT THREE COPIES OF ONE ``if``
=========================================================
The gate that shipped in M0.x understood exactly one category: the database. It
worked — and that is what made it dangerous, because it read as "CI cannot
silently skip" when what it actually guaranteed was "CI cannot silently skip a
DATABASE test". T-FL-03, the object-storage manifest handoff that makes the
Explorer fleet horizontally scalable, had six tests that CI never once executed:
no S3 service existed, every one of them skipped, and the database-shaped gate
looked straight past them because their skip reason named ``QEC_TEST_S3_ENDPOINT``
rather than a DSN. The build was green and had proven nothing.

A hardcoded gate does not fail when a new infrastructure category arrives — it
stays quiet, which is the same failure wearing the same green. So categories are
DECLARED here, and adding one is a registration rather than an edit to the
detection logic. :func:`register_infra_category` is the seam; the canary in
``tests/contract/test_infra_skip_gate_canary.py`` is the proof the seam works.

REGISTERING A NEW INFRASTRUCTURE CATEGORY
=========================================
Three things, in this file::

    InfraCategory(
        key="rabbitmq",                       # short handle used in messages
        label="RabbitMQ",                     # what a human calls it
        require_env="QEC_REQUIRE_RABBITMQ",   # the CI "this is mandatory" flag
        skip_signatures=("QEC_TEST_AMQP_URL",),  # substrings that identify the
                                                 # skip as "I had no RabbitMQ"
        remedy="wire QEC_TEST_AMQP_URL to the CI RabbitMQ service",
    )

then set the flag in the CI job that provisions it. Nothing else changes: the
detection, the reporting and the canary are category-agnostic.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field


def _flag(name: str) -> bool:
    """A ``QEC_REQUIRE_*`` flag, read with the house truthiness rules."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class InfraCategory:
    """One class of infrastructure CI may declare mandatory."""

    #: Short handle (``db``, ``redis``, ``s3``) used by the test-side helpers.
    key: str
    #: Human name, used in the failure report.
    label: str
    #: The environment flag that makes this category mandatory for a run.
    require_env: str
    #: Substrings that identify a skip reason as "I had no <this>". Matched
    #: against the skip's reason text, so the reason must NAME the variable —
    #: which the gate helpers below guarantee for anything they produce.
    skip_signatures: tuple[str, ...]
    #: Substrings that EXEMPT a skip whose reason also matches a signature. See
    #: the database entry for the one case this exists for.
    exempt_signatures: tuple[str, ...] = ()
    #: What the operator should do about it, printed with the failure.
    remedy: str = ""
    #: Environment variables a test module reads to find this infrastructure.
    #: Declarative only — used by the drift test, never by the detection.
    env_vars: tuple[str, ...] = field(default_factory=tuple)

    def required(self) -> bool:
        return _flag(self.require_env)

    def matches(self, reason: str) -> bool:
        if any(sig in reason for sig in self.exempt_signatures):
            return False
        return any(sig in reason for sig in self.skip_signatures)


# ─── The registry ───────────────────────────────────────────────────────────

_DATABASE = InfraCategory(
    key="db",
    label="Database (PostgreSQL)",
    require_env="QEC_REQUIRE_DB",
    # NOTE ``QEC_TEST_REDIS_URL`` is deliberately listed HERE as well as under
    # the Redis category. It was a database signature before A27.1 existed, so
    # removing it would have QUIETLY WEAKENED the existing gate: the qec-database
    # job sets QEC_REQUIRE_DB, and a Redis skip there stopped being a failure the
    # moment Redis moved to a flag that job did not set. Enforcement is a union,
    # never a hand-off — either flag catches it.
    skip_signatures=(
        "QEC_TEST_DATABASE_URL",
        "QEC_TEST_QEC_DATABASE_URL",
        "QEC_TEST_SUBSTRATE_DATABASE_URL",
        "QEC_TEST_ADMIN_DATABASE_URL",
        "QEC_TEST_REDIS_URL",
        "BYPASSES RLS",
    ),
    # …and substrings that EXEMPT a skip even though its reason also names a
    # DSN. QEC_REQUIRE_DB is a promise about the DATABASE services; it is not a
    # promise that a live platform-api HTTP server is running beside them. The
    # §2.4 factory HTTP honesty pins need one ("the live factory sharing the same
    # DB"), so under QEC_REQUIRE_DB alone they legitimately skip — treating them
    # as database failures would make the gate cry wolf, and a gate that cries
    # wolf gets switched off. Standing up that service is its own milestone;
    # until then this exemption is the honest declaration of the boundary.
    exempt_signatures=("QEC_TEST_PLATFORM_API_URL",),
    remedy=("wire the QEC_TEST_*_DATABASE_URL variables to the CI Postgres "
            "service, or unset QEC_REQUIRE_DB"),
    env_vars=("QEC_TEST_DATABASE_URL", "QEC_TEST_QEC_DATABASE_URL",
              "QEC_TEST_SUBSTRATE_DATABASE_URL", "QEC_TEST_ADMIN_DATABASE_URL"),
)

_REDIS = InfraCategory(
    key="redis",
    label="Redis",
    require_env="QEC_REQUIRE_REDIS",
    skip_signatures=("QEC_TEST_REDIS_URL",),
    remedy="wire QEC_TEST_REDIS_URL to the CI Redis service, or unset QEC_REQUIRE_REDIS",
    env_vars=("QEC_TEST_REDIS_URL",),
)

_S3 = InfraCategory(
    key="s3",
    label="S3 / MinIO object storage",
    require_env="QEC_REQUIRE_S3",
    skip_signatures=("QEC_TEST_S3_ENDPOINT", "QEC_TEST_S3_BUCKET"),
    remedy=("wire QEC_TEST_S3_ENDPOINT to the CI MinIO service, or unset "
            "QEC_REQUIRE_S3"),
    env_vars=("QEC_TEST_S3_ENDPOINT", "QEC_TEST_S3_BUCKET"),
)

INFRA_CATEGORIES: list[InfraCategory] = [_DATABASE, _REDIS, _S3]


def register_infra_category(category: InfraCategory) -> InfraCategory:
    """Add a category to the gate. THE extension seam — see the module docstring.

    Idempotent on ``key`` so a plugin loaded twice cannot double-report.
    """
    for i, existing in enumerate(INFRA_CATEGORIES):
        if existing.key == category.key:
            INFRA_CATEGORIES[i] = category
            return category
    INFRA_CATEGORIES.append(category)
    return category


def get_infra_category(key: str) -> InfraCategory:
    for c in INFRA_CATEGORIES:
        if c.key == key:
            return c
    raise KeyError(
        f"unknown infrastructure category {key!r}; registered: "
        f"{[c.key for c in INFRA_CATEGORIES]}")


def infra_required(key: str) -> bool:
    """True when CI has declared this infrastructure MANDATORY for the run."""
    return get_infra_category(key).required()


# ─── Test-side helpers (the two-state gate) ─────────────────────────────────


def infra_gate(value: str, env_name: str, purpose: str, category: str):
    """A skipif mark that STOPS being a skip once the category is required.

    With the endpoint present the mark never skips. With it absent the mark skips
    on a laptop and does NOT skip in CI — there the test body's
    :func:`require_infra` turns the absence into a failure that names the
    variable. The two halves are deliberate: the mark handles the laptop, the
    assertion is what makes a missing service RED rather than silent.
    """
    import pytest

    return pytest.mark.skipif(
        not value and not infra_required(category),
        reason=f"{env_name} not set — {purpose}",
    )


def require_infra(value: str, env_name: str, category: str) -> str:
    """Assert the endpoint is present; return it. The CI-side half of the gate."""
    cat = get_infra_category(category)
    assert value, (
        f"{env_name} is not set, but {cat.require_env} declares "
        f"{cat.label} MANDATORY for this run. An infrastructure-gated test that "
        f"cannot execute is a CI FAILURE, never a silent skip — {cat.remedy}."
    )
    return value


# ─── The session gate (pytest hooks) ────────────────────────────────────────
#
# Usable two ways, and BOTH are load-bearing:
#   * imported by tests/conftest.py, which is how it guards the real suite;
#   * loaded directly with `pytest -p _infra_gate`, which is how the canary in
#     tests/contract/test_infra_skip_gate_canary.py drives a throwaway session
#     against THIS code rather than against a re-implementation of it.

#: category key -> [(nodeid, reason), …]
_skips: dict[str, list[tuple[str, str]]] = {}


def _reason_of(report) -> str:
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def pytest_runtest_logreport(report):
    """Record every skip whose reason names a registered infrastructure.

    BOTH the setup and call phases are watched. A ``@skipif`` mark reports at
    setup; a ``pytest.skip()`` raised inside a test body reports at call — and
    the second is just as silent as the first, so a gate that watched only setup
    left a hole a test could walk straight through.
    """
    if report.when not in ("setup", "call") or report.outcome != "skipped":
        return
    reason = _reason_of(report).replace("Skipped: ", "", 1)
    for category in INFRA_CATEGORIES:
        if category.matches(reason):
            bucket = _skips.setdefault(category.key, [])
            if not any(n == report.nodeid for n, _ in bucket):
                bucket.append((report.nodeid, reason))


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook shape
    """Fail the session when tests for REQUIRED infrastructure could not execute."""
    offenders = [
        (c, _skips.get(c.key, []))
        for c in INFRA_CATEGORIES
        if c.required() and _skips.get(c.key)
    ]
    if not offenders:
        return
    blocks = []
    for category, entries in offenders:
        listed = "\n".join(f"    {nodeid}\n      ↳ {reason}"
                           for nodeid, reason in entries)
        blocks.append(
            f"{category.require_env} is set, but {len(entries)} "
            f"{category.label}-gated test(s) SKIPPED.\n"
            f"  Remedy: {category.remedy}\n{listed}"
        )
    total = sum(len(e) for _, e in offenders)
    print(
        "\n"
        "═══════════════════════════════════════════════════════════════════\n"
        f"NO-SILENT-SKIP GATE: {total} test(s) for REQUIRED infrastructure "
        f"never executed.\n"
        "A skipped infrastructure test is NOT a passing infrastructure test.\n"
        "The services were declared mandatory for this run, so this is a "
        "FAILURE:\n\n"
        + "\n\n".join(blocks) +
        "\n═══════════════════════════════════════════════════════════════════",
        file=sys.stderr,
    )
    session.exitstatus = 1


def reset_for_tests() -> None:
    """Drop recorded skips. For the gate's own unit tests only."""
    _skips.clear()

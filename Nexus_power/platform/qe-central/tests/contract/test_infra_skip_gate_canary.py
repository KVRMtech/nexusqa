"""A27.2 — the CANARY that proves the no-silent-skip gate is not decorative.

WHY A CANARY AT ALL
===================
A gate without proof is not a gate. The database half of this gate existed for a
whole milestone and looked like it guaranteed "CI cannot silently skip". What it
actually guaranteed was "CI cannot silently skip a DATABASE test" — and six
object-storage tests skipped in every run ever made, underneath a green build,
without anyone noticing. A gate that has never been SEEN to fire is
indistinguishable from a gate that cannot.

So this file does to the skip detector what
``test_rls_coverage_complete.py::…canary…`` does to the RLS coverage detector: it
deliberately creates the exact violation the detector exists to catch, and fails
if the detector stays quiet.

HOW THE PROOF IS CONSTRUCTED
============================
The violation cannot be staged in THIS session — a session that fails itself is
not a test, it is a broken build. So each arm runs a throwaway pytest session in
a SUBPROCESS:

    a synthetic S3-gated test  ->  no endpoint  ->  it skips
        ->  the REAL gate, loaded as `-p _infra_gate`
            ->  inner session exits NON-ZERO
                ->  this outer test passes because it did

``-p _infra_gate`` matters. The inner session loads the SAME module that guards
the real suite from ``tests/conftest.py``; a canary that exercised a copy of the
detection logic would only ever prove that the copy works.

WHAT EACH ARM PINS
==================
  1. required + skipped            -> RED     (the headline: silence is impossible)
  2. not required + skipped        -> GREEN   (a laptop may still skip)
  3. required + unrelated skip     -> GREEN   (the gate must not cry wolf)
  4. required + a category invented at runtime -> RED (the registry is the seam)
  5. the database category still behaves exactly as it did before A27.1
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# tests/ (which holds _infra_gate.py) — the inner sessions import it from here.
_TESTS_DIR = Path(__file__).resolve().parents[1]

#: Every flag the gate understands. The inner env is built from a SCRUBBED copy
#: of this process's environment: the qec-database job runs this file with
#: QEC_REQUIRE_DB / QEC_REQUIRE_REDIS / QEC_REQUIRE_S3 all set, and inheriting
#: them would make arm 2 ("a laptop may still skip") fail for the wrong reason —
#: the canary would be testing the CI job's env instead of the gate.
_REQUIRE_FLAGS = ("QEC_REQUIRE_DB", "QEC_REQUIRE_REDIS", "QEC_REQUIRE_S3")

#: …and the endpoints. A CI job that HAS provisioned MinIO exports
#: QEC_TEST_S3_ENDPOINT, and an inner session that inherited it would not skip at
#: all, quietly turning this canary into a no-op that always passes.
_ENDPOINT_VARS = (
    "QEC_TEST_S3_ENDPOINT", "QEC_TEST_S3_BUCKET",
    "QEC_TEST_DATABASE_URL", "QEC_TEST_QEC_DATABASE_URL",
    "QEC_TEST_SUBSTRATE_DATABASE_URL", "QEC_TEST_ADMIN_DATABASE_URL",
    "QEC_TEST_REDIS_URL",
)


def _inner_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in _REQUIRE_FLAGS and k not in _ENDPOINT_VARS}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_TESTS_DIR), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    # The inner session asserts on stderr text; force UTF-8 so the box-drawing
    # characters in the report cannot die on a cp1252 console.
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(overrides)
    return env


def _run_inner(tmp_path: Path, body: str, *, conftest: str = "",
               **env: str) -> subprocess.CompletedProcess:
    """Run a throwaway pytest session against `body`, guarded by the REAL gate."""
    # Its own pytest.ini so the inner rootdir is this temp directory and not
    # whatever config happens to sit above the checkout (a stray pyproject.toml
    # in the developer's home directory is enough to change addopts).
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_canary_probe.py").write_text(
        textwrap.dedent(body), encoding="utf-8")
    if conftest:
        (tmp_path / "conftest.py").write_text(
            textwrap.dedent(conftest), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-p", "_infra_gate",
         "-p", "no:cacheprovider", "-p", "no:randomly", "-q"],
        cwd=str(tmp_path), env=_inner_env(**env),
        capture_output=True, timeout=300,
        # Decode EXPLICITLY as UTF-8. `text=True` decodes with the parent's
        # locale encoding, which on a Windows console is cp1252 and dies on the
        # box-drawing characters in the gate's own report — the canary would
        # then fail on the developer machines it exists to protect.
        encoding="utf-8", errors="replace",
    )


#: The violation itself: a test gated on an S3 endpoint that is not there. This
#: is deliberately a PLAIN skipif rather than the two-state `infra_gate` helper —
#: the point of this canary is the SESSION-level detector, which must catch a
#: skip no matter how a test module chose to spell its gate.
_S3_SKIP_PROBE = '''
    import pytest

    @pytest.mark.skipif(True, reason="QEC_TEST_S3_ENDPOINT not set - "
                                     "needs a real S3-compatible endpoint")
    def test_needs_object_storage():
        raise AssertionError("must not execute - it is meant to skip")

    def test_something_that_actually_runs():
        assert True
'''


# ══════════════════════════════════════════════════════════════════════════
# ARM 1 — THE HEADLINE. Required infrastructure + a skip == a RED build.
# ══════════════════════════════════════════════════════════════════════════

def test_a_skipped_s3_test_fails_ci_when_s3_is_required(tmp_path):
    """infrastructure unavailable -> skipped -> detected -> CI FAILS."""
    r = _run_inner(tmp_path, _S3_SKIP_PROBE, QEC_REQUIRE_S3="1")
    out = r.stdout + r.stderr

    assert r.returncode != 0, (
        "THE GATE IS VACUOUS. QEC_REQUIRE_S3 was set and an S3-gated test "
        "SKIPPED, and the session still exited 0 — which is exactly the "
        "condition that let six T-FL-03 tests never execute under a green "
        f"build.\n--- inner session ---\n{out}"
    )
    assert "NO-SILENT-SKIP GATE" in out, (
        f"the session failed, but not with the gate's report:\n{out}")
    assert "S3 / MinIO object storage" in out, (
        f"the report did not name the S3 category:\n{out}")
    assert "test_needs_object_storage" in out, (
        f"the report did not name the offending test:\n{out}")
    # The skip must still be REPORTED as a skip — the gate observes, it does not
    # rewrite outcomes, so the underlying pytest result stays truthful.
    assert "1 passed" in out and "1 skipped" in out, (
        f"the gate altered the inner session's own outcomes:\n{out}")


# ══════════════════════════════════════════════════════════════════════════
# ARM 2 — THE OTHER DIRECTION. A laptop with no MinIO may still skip.
# ══════════════════════════════════════════════════════════════════════════

def test_the_same_skip_is_green_when_s3_is_not_required(tmp_path):
    """Without the flag this is a developer without MinIO, which is fine.

    Without this arm the gate could "pass" arm 1 by failing every session that
    contains any skip at all, which would be useless and would be switched off
    within a week.
    """
    r = _run_inner(tmp_path, _S3_SKIP_PROBE)
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        "a skipped S3 test failed the session even though QEC_REQUIRE_S3 was "
        f"NOT set — the gate fires on laptops:\n{out}")
    assert "NO-SILENT-SKIP GATE" not in out


# ══════════════════════════════════════════════════════════════════════════
# ARM 3 — NO CRYING WOLF. An unrelated skip is not an infrastructure skip.
# ══════════════════════════════════════════════════════════════════════════

def test_an_unrelated_skip_does_not_fail_a_required_run(tmp_path):
    """A gate that fails on every skip is noise, and noise gets disabled."""
    body = '''
        import pytest

        @pytest.mark.skipif(True, reason="opt-in slow test; run with -m slow")
        def test_slow_thing():
            raise AssertionError("must not execute")
    '''
    r = _run_inner(tmp_path, body, QEC_REQUIRE_S3="1", QEC_REQUIRE_DB="1",
                   QEC_REQUIRE_REDIS="1")
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        "a skip that has nothing to do with infrastructure failed the run — "
        f"the gate cries wolf:\n{out}")
    assert "NO-SILENT-SKIP GATE" not in out


def test_the_platform_api_exemption_survives(tmp_path):
    """The one documented exemption must still exempt.

    QEC_REQUIRE_DB promises the DATABASE services, not a live platform-api HTTP
    server. This exemption predates A27.1 and losing it in the generalisation
    would have turned a deliberate boundary into a false failure.
    """
    body = '''
        import pytest

        @pytest.mark.skipif(True, reason="QEC_TEST_PLATFORM_API_URL not set - "
                                         "needs the live factory sharing the "
                                         "same QEC_TEST_DATABASE_URL")
        def test_factory_http_honesty():
            raise AssertionError("must not execute")
    '''
    r = _run_inner(tmp_path, body, QEC_REQUIRE_DB="1")
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"the documented platform-api exemption was lost in A27.1:\n{out}")


# ══════════════════════════════════════════════════════════════════════════
# ARM 4 — THE SEAM. A category invented at runtime is enforced identically.
# ══════════════════════════════════════════════════════════════════════════

def test_a_newly_registered_infrastructure_category_is_enforced(tmp_path):
    """"Future infrastructure types must be easy to register" — proven, not claimed.

    The inner session registers a category the gate has never heard of and does
    NOT touch the detection logic. If registration alone is enough to make a skip
    fatal, the framework is genuinely extensible; if it is not, this fails and
    the next dependency would have arrived with the same silent hole S3 did.
    """
    conftest = '''
        from _infra_gate import InfraCategory, register_infra_category

        register_infra_category(InfraCategory(
            key="rabbitmq",
            label="RabbitMQ",
            require_env="QEC_REQUIRE_RABBITMQ",
            skip_signatures=("QEC_TEST_AMQP_URL",),
            remedy="wire QEC_TEST_AMQP_URL to the CI RabbitMQ service",
        ))
    '''
    body = '''
        import pytest

        @pytest.mark.skipif(True, reason="QEC_TEST_AMQP_URL not set - "
                                         "needs a real broker")
        def test_needs_a_broker():
            raise AssertionError("must not execute")
    '''
    r = _run_inner(tmp_path, body, conftest=conftest, QEC_REQUIRE_RABBITMQ="1")
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        "registering a new infrastructure category did NOT make its skips "
        f"fatal — the registry is not actually the extension seam:\n{out}")
    assert "RabbitMQ" in out and "test_needs_a_broker" in out, out

    # …and it must stay inert while its own flag is unset.
    r2 = _run_inner(tmp_path, body, conftest=conftest, QEC_REQUIRE_S3="1")
    assert r2.returncode == 0, (
        "an unset QEC_REQUIRE_RABBITMQ still failed the run:\n"
        f"{r2.stdout + r2.stderr}")


# ══════════════════════════════════════════════════════════════════════════
# ARM 5 — NO REGRESSION. The database and Redis halves still behave as before.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("dsn_var", [
    "QEC_TEST_DATABASE_URL",
    "QEC_TEST_QEC_DATABASE_URL",
    "QEC_TEST_SUBSTRATE_DATABASE_URL",
    "QEC_TEST_ADMIN_DATABASE_URL",
    "QEC_TEST_REDIS_URL",
])
def test_database_enforcement_is_unchanged(tmp_path, dsn_var):
    """Every DSN the M0.x gate caught under QEC_REQUIRE_DB is still caught."""
    body = f'''
        import pytest

        @pytest.mark.skipif(True, reason="{dsn_var} not set - needs the CI database")
        def test_needs_a_database():
            raise AssertionError("must not execute")
    '''
    r = _run_inner(tmp_path, body, QEC_REQUIRE_DB="1")
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"QEC_REQUIRE_DB no longer catches a skip naming {dsn_var} — A27.1 "
        f"weakened the gate it was supposed to generalise:\n{out}")
    assert "NO-SILENT-SKIP GATE" in out


def test_redis_is_enforceable_on_its_own_flag(tmp_path):
    """The NEW axis: Redis can be declared mandatory without the database."""
    body = '''
        import pytest

        @pytest.mark.skipif(True, reason="QEC_TEST_REDIS_URL not set - needs Redis")
        def test_needs_redis():
            raise AssertionError("must not execute")
    '''
    r = _run_inner(tmp_path, body, QEC_REQUIRE_REDIS="1")
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"QEC_REQUIRE_REDIS does not enforce:\n{out}"
    assert "Redis" in out


def test_a_call_phase_skip_is_caught_too(tmp_path):
    """``pytest.skip()`` inside a test body is just as silent as a mark.

    The M0.x gate watched only the setup phase, so a runtime skip walked
    straight through it.
    """
    body = '''
        import pytest

        def test_gives_up_halfway():
            pytest.skip("QEC_TEST_S3_ENDPOINT not set - no object storage")
    '''
    r = _run_inner(tmp_path, body, QEC_REQUIRE_S3="1")
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"a call-phase skip escaped the gate:\n{out}")
    assert "test_gives_up_halfway" in out


# ══════════════════════════════════════════════════════════════════════════
# DRIFT — the registry and the DB gate must not describe different worlds.
# ══════════════════════════════════════════════════════════════════════════

def test_the_registry_covers_every_dsn_the_db_gate_knows_about():
    """``_dbgate.DB_ENV_VARS`` is where DB-gated modules get their DSN names.

    If a DSN is added there and not here, tests gated on it would skip and the
    gate would not notice — the S3 hole again, with a different variable name.
    """
    import _dbgate
    from _infra_gate import get_infra_category

    db = get_infra_category("db")
    # Subject-presence: an empty (or stubbed) DB_ENV_VARS makes `missing` empty
    # and this test green while comparing nothing at all.
    assert len(_dbgate.DB_ENV_VARS) >= 4, (
        f"_dbgate.DB_ENV_VARS holds {_dbgate.DB_ENV_VARS} — too few to be the "
        f"real list, so this drift check has no subject")
    assert "QEC_TEST_QEC_DATABASE_URL" in _dbgate.DB_ENV_VARS
    missing = [v for v in _dbgate.DB_ENV_VARS if v not in db.skip_signatures]
    assert not missing, (
        f"these DSN variables are known to _dbgate but invisible to the "
        f"no-silent-skip gate: {missing}")


def test_every_registered_category_is_well_formed():
    from _infra_gate import INFRA_CATEGORIES

    keys = [c.key for c in INFRA_CATEGORIES]
    # Subject-presence: every assertion below is a loop over INFRA_CATEGORIES,
    # so an empty registry would satisfy all of them.
    assert {"db", "redis", "s3"} <= set(keys), (
        f"the registry is missing a core category; found {keys}")
    assert len(keys) == len(set(keys)), f"duplicate category keys: {keys}"
    for c in INFRA_CATEGORIES:
        assert c.require_env.startswith("QEC_REQUIRE_"), c
        assert c.skip_signatures, f"{c.key} declares no skip signatures"
        assert c.remedy, f"{c.key} tells the operator nothing about the fix"


# ══════════════════════════════════════════════════════════════════════════
# THE RESIDUAL RISK, CLOSED — a skip reason that does not NAME its variable
# ══════════════════════════════════════════════════════════════════════════
#
# The gate recognises an infrastructure skip by the environment variable in its
# REASON. That is a deliberate, cheap and robust contract — but it has one edge:
# a hand-written reason that describes the dependency in prose instead of naming
# it is invisible to the gate, no matter how correct the gate is.
#
# This was not hypothetical. Auditing all 148 skips in the suite while building
# A27 found THREE modules gated like this:
#
#     reason="T-FL-01 needs the qecentral + substrate test DSNs"
#
# — eighteen fleet tests that would have skipped SILENTLY under QEC_REQUIRE_DB if
# the CI database had failed to start. The same defect as T-FL-03, on the
# database axis, still open after the gate that was supposed to close it.
#
# Fixing those three reasons fixes today. This test fixes tomorrow: it reads the
# suite's own source and fails if any skip reason talks about infrastructure
# without naming a variable the gate can see.

_INFRA_VOCAB = ("dsn", "postgres", "database", "redis", "minio", "s3 ", "s3-",
                "object storage", "bucket")


def _static_reason(call):
    """The reason of a `skipif(...)`/`skip(...)` call, when it is a literal.

    Non-literal reasons are deliberately ignored: those come from `infra_gate()`
    / `db_gate()`, which BUILD the reason from the variable name and therefore
    cannot omit it.
    """
    import ast

    for kw in call.keywords:
        if (kw.arg == "reason" and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)):
            return kw.value.value
    name = (call.func.attr if isinstance(call.func, ast.Attribute)
            else getattr(call.func, "id", ""))
    if (name == "skip" and call.args and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)):
        return call.args[0].value
    return None


def test_no_skip_reason_describes_infrastructure_without_naming_it():
    import ast

    from _infra_gate import INFRA_CATEGORIES

    signatures = {s for c in INFRA_CATEGORIES for s in c.skip_signatures}
    signatures |= {s for c in INFRA_CATEGORIES for s in c.exempt_signatures}

    # SUBJECT-PRESENCE CONTROL. "Would this check still pass if the subject were
    # absent?" — for a scanner, yes: a wrong root, a broken glob or a rename of
    # every test file leaves `offenders` empty and the test green while it has
    # examined nothing. The counters below make the scan prove it ran.
    scanned = reasons_seen = matched = 0
    saw_known_module = False
    unparseable: list[str] = []

    offenders = []
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            # NOT `continue`. Swallowing this made the scanner a silent skip of
            # exactly the kind it exists to detect: a module that will not parse
            # is simply not examined, and the guard goes green having quietly
            # dropped it. Found the hard way — a syntax error introduced in the
            # T-FL-03 proof was swallowed here, and only the known-module anchor
            # below noticed the file had vanished from the scan.
            unparseable.append(
                f"{path.relative_to(_TESTS_DIR).as_posix()}: {exc}")
            continue
        scanned += 1
        if path.name == "test_t_fl_03_object_storage_handoff.py":
            saw_known_module = True
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                     else getattr(node.func, "id", ""))
            if fname not in ("skipif", "skip"):
                continue
            reason = _static_reason(node)
            if not reason:
                continue
            reasons_seen += 1
            if any(s in reason for s in signatures):
                matched += 1
            mentions = any(v in reason.lower() for v in _INFRA_VOCAB)
            if mentions and not any(s in reason for s in signatures):
                offenders.append(
                    f"{path.relative_to(_TESTS_DIR).as_posix()}:{node.lineno}\n"
                    f"      ↳ {reason[:140]}")

    assert not unparseable, (
        "these test modules could not be parsed, so the guard could not examine "
        "them — a module the scanner cannot read is a module the scanner is "
        "silently skipping:" + "\n    "
        + ("\n    ").join(unparseable))

    # …and the RIGHT subject. "50 modules parsed" is satisfied by any fifty
    # modules; anchoring on a file that must be in scope catches a scan pointed
    # at a directory that merely resembles this suite.
    assert saw_known_module, (
        f"the scan parsed {scanned} modules but not the T-FL-03 handoff proof — "
        f"it is looking at the wrong tree")
    assert scanned >= 50, (
        f"the scan only parsed {scanned} test modules under {_TESTS_DIR} — it is "
        f"not looking at the suite, so a green result here means nothing")
    assert reasons_seen >= 20, (
        f"the scan found only {reasons_seen} literal skip reasons; it is not "
        f"extracting them, so it cannot be finding offenders either")
    assert matched >= 10, (
        f"only {matched} skip reasons matched a registered infrastructure "
        f"signature — the matcher is not seeing real content, so 'no offenders' "
        f"would be an artefact of the matcher, not a fact about the suite")

    assert not offenders, (
        "these skip reasons describe infrastructure but name no variable the "
        "no-silent-skip gate can recognise, so the tests behind them would skip "
        "SILENTLY in a CI run that declared that infrastructure mandatory:\n    "
        + "\n    ".join(offenders)
        + "\n\n  Fix: name the environment variable in the reason (or build the "
          "mark with _infra_gate.infra_gate / _dbgate.db_gate, which name it "
          "for you)."
    )

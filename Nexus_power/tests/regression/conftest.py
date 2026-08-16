"""
pytest scaffolding for the canonical regression suite.

Responsibilities:
  - Load the manifest in fixtures.yaml and expose fixtures keyed by id.
  - Fetch the media files from the configured fixture URL on first use,
    caching them under ./.cache so repeated runs don't re-download.
  - Provide an httpx client wired to the orchestrator under test.
  - Provide a golden-comparison helper with field-aware tolerances.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml


HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / ".cache"
GOLDEN_DIR = HERE / "golden"


# ─── Manifest loading ──────────────────────────────────────────


@dataclass
class Fixture:
    id: str
    kind: str
    source: str
    sha256: str
    duration_seconds: float | None
    description: str
    profile: str


def _resolve_env(value: str) -> str:
    """Expand ${VAR} references in manifest strings."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


def load_manifest() -> dict:
    raw = yaml.safe_load((HERE / "fixtures.yaml").read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})
    base = _resolve_env(defaults.get("source_base", ""))
    profile = defaults.get("profile", "fast")
    out: dict[str, Fixture] = {}
    for f in raw["fixtures"]:
        src = f["source"]
        if not src.startswith(("http://", "https://", "s3://", "gs://", "file://")):
            src = f"{base.rstrip('/')}/{src.lstrip('/')}"
        out[f["id"]] = Fixture(
            id=f["id"],
            kind=f["kind"],
            source=src,
            sha256=f["sha256"],
            duration_seconds=f.get("duration_seconds"),
            description=f.get("description", ""),
            profile=f.get("profile", profile),
        )
    return out


def _fetch_to_cache(src: str, sha256: str) -> Path:
    """Resolve a fixture source to a local file in CACHE_DIR, verifying sha256."""
    CACHE_DIR.mkdir(exist_ok=True)
    name = sha256[:16] + Path(src).suffix
    target = CACHE_DIR / name
    if target.is_file():
        return target

    if src.startswith("s3://"):
        import boto3  # boto3 is a test-only dep; install via tests requirements

        bucket, key = src[5:].split("/", 1)
        boto3.client("s3").download_file(bucket, key, str(target))
    elif src.startswith(("http://", "https://")):
        with httpx.stream("GET", src, timeout=120) as r:
            r.raise_for_status()
            with target.open("wb") as fh:
                for chunk in r.iter_bytes(1024 * 1024):
                    fh.write(chunk)
    elif src.startswith("file://"):
        local = Path(src[7:])
        target.write_bytes(local.read_bytes())
    else:
        raise RuntimeError(f"unsupported fixture source scheme: {src}")

    # Verify sha256 unless the manifest leaves it as the all-zeros sentinel
    # (signalling that the fixture is intentionally unpinned, e.g. during
    # initial onboarding).
    if sha256 != "0" * 64:
        h = hashlib.sha256()
        with target.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != sha256:
            target.unlink(missing_ok=True)
            raise AssertionError(
                f"fixture {src} sha256 mismatch: expected {sha256} got {h.hexdigest()}"
            )
    return target


# ─── pytest fixtures ───────────────────────────────────────────


@pytest.fixture(scope="session")
def manifest() -> dict[str, Fixture]:
    return load_manifest()


@pytest.fixture(scope="session")
def fixture_loader(manifest):
    def load(fixture_id: str) -> Path:
        if fixture_id not in manifest:
            raise KeyError(f"unknown fixture {fixture_id!r}")
        f = manifest[fixture_id]
        # The manifest's source_base is ${NEXUS_REGRESSION_FIXTURE_URL}. Unset,
        # it expands to "" and every source degrades to a bare relative path, so
        # the fetcher died with the opaque "unsupported fixture source scheme:
        # /audio/kt-call-en-01.wav". SKIP with the real reason instead: this
        # suite replays a hosted MEDIA CORPUS, and no corpus means the assertions
        # cannot run — which is a missing input, not a regression. Point
        # NEXUS_REGRESSION_FIXTURE_URL at the bucket/mirror and they execute.
        if not f.source.startswith(("http://", "https://", "s3://", "gs://", "file://")):
            pytest.skip(
                "NEXUS_REGRESSION_FIXTURE_URL is not set — the regression media "
                f"corpus is unavailable, so fixture {fixture_id!r} cannot be "
                f"fetched (resolved to {f.source!r})"
            )
        return _fetch_to_cache(f.source, f.sha256)

    return load


@pytest.fixture(scope="session")
def orchestrator_url() -> str:
    return os.environ.get(
        "NEXUS_ORCHESTRATOR_URL", "http://localhost:8100"
    )


@pytest.fixture
def orchestrator_client(orchestrator_url):
    token = os.environ.get("NEXUS_REGRESSION_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=orchestrator_url, timeout=60, headers=headers) as client:
        yield client


# ─── Golden comparison helper ─────────────────────────────────


def _normalised_lev(a: str, b: str) -> float:
    """Simple normalised Levenshtein distance. Pure Python, no extra deps."""
    if a == b:
        return 0.0
    if not a or not b:
        return 1.0
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            prev, dp[j] = dp[j], min(
                dp[j] + 1,
                dp[j - 1] + 1,
                prev + (0 if a[i - 1] == b[j - 1] else 1),
            )
    return dp[n] / max(m, n)


def _compare(field: str, expected: Any, actual: Any, tolerance: dict[str, Any]) -> str | None:
    """Returns an error message or None on success."""
    kind = tolerance.get("kind") if isinstance(tolerance, dict) else None
    if kind == "string":
        max_lev = float(tolerance.get("max_lev", 0.05))
        d = _normalised_lev(str(expected), str(actual))
        if d > max_lev:
            return f"{field}: levenshtein {d:.3f} > {max_lev}"
        return None
    if kind == "number" or isinstance(expected, (int, float)):
        if not isinstance(actual, (int, float)):
            return f"{field}: expected number, got {type(actual).__name__}"
        rel = float(tolerance.get("max_rel", 0.05)) if isinstance(tolerance, dict) else 0.05
        if expected == 0:
            return None if abs(actual) <= float(tolerance.get("abs", 0)) else f"{field}: expected 0, got {actual}"
        if abs(actual - expected) / abs(expected) > rel:
            return f"{field}: relative drift {(actual - expected) / expected:.3f} > {rel}"
        return None
    if kind == "list_count" or isinstance(expected, list):
        e = expected if isinstance(expected, int) else len(expected)
        a = len(actual) if isinstance(actual, list) else int(actual)
        bound = int(tolerance.get("abs", 1)) if isinstance(tolerance, dict) else 1
        if abs(a - e) > bound:
            return f"{field}: list-count {a} vs {e} > ±{bound}"
        return None
    return None if expected == actual else f"{field}: {expected!r} != {actual!r}"


def assert_matches_golden(fixture_id: str, result: dict, *, strict: bool = True) -> None:
    """
    Compare `result` against tests/regression/golden/<fixture_id>.json.
    A `_tolerances` block in the golden tweaks per-field tolerances.

    Raises AssertionError with a summary of all field violations.
    """
    golden_path = GOLDEN_DIR / f"{fixture_id}.json"
    if not golden_path.is_file():
        if strict:
            raise AssertionError(f"missing golden: {golden_path}")
        return
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    tolerances = golden.pop("_tolerances", {}) if isinstance(golden, dict) else {}

    errors: list[str] = []
    for field, expected in golden.items():
        actual = result.get(field) if isinstance(result, dict) else None
        msg = _compare(field, expected, actual, tolerances.get(field, {}))
        if msg:
            errors.append(msg)
    if errors:
        raise AssertionError(f"regression: {fixture_id} → " + "; ".join(errors))

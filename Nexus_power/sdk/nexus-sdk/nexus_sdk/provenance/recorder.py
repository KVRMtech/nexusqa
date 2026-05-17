"""Pipeline-run provenance recorder.

Captures the exact toolchain that produced a canonical artifact so
later replay or audit can compare against it.  Pure transformation
plus a few cheap environment lookups (env vars, optional ``git rev-parse``);
no DB, no network, no external services — safe to call inline at the
end of any chain stage.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ─── Public types ────────────────────────────────────────────────────────────

@dataclass
class PipelineRunProvenance:
    """Reproducibility manifest for one canonical-artifact run.

    The fields are intentionally flat so the dict serialisation is
    diff-friendly — comparing two ``to_dict()`` outputs immediately
    surfaces what changed across two runs.
    """

    artifact_id: str
    chain_id: str
    chain_version: str
    pipeline_version: str
    sdk_version: str

    git_commit: Optional[str]
    deployment_env: str

    # Engine + model resolution.  ``model_resolved`` is the *exact*
    # model id the cloud provider routed to (Anthropic / OpenAI may
    # alias claude-sonnet-4-6 → an internal revision); pinning that
    # here lets us replay against the same revision later.
    engine_versions: dict[str, str] = field(default_factory=dict)
    model_resolved: dict[str, str] = field(default_factory=dict)
    model_providers: dict[str, str] = field(default_factory=dict)

    # SDK sub-module versions used at run time.  Most modules don't
    # version themselves explicitly; we use the package's ``__version__``
    # attribute when present, falling back to the parent SDK version.
    sdk_modules: dict[str, str] = field(default_factory=dict)

    # Pipeline configuration captured for this run.  feature_flags is
    # the exact set of EYES_*/SHIELD_*/HEART_* env booleans the run saw.
    processing_profile: str = ""
    feature_flags: dict[str, bool] = field(default_factory=dict)
    env_fingerprint: dict[str, str] = field(default_factory=dict)

    # Runtime fingerprint — Python version, platform, container ID etc.
    runtime: dict[str, str] = field(default_factory=dict)

    captured_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Stable serialisable form for storage in artifact JSON."""
        return {
            "artifact_id": self.artifact_id,
            "chain_id": self.chain_id,
            "chain_version": self.chain_version,
            "pipeline_version": self.pipeline_version,
            "sdk_version": self.sdk_version,
            "git_commit": self.git_commit,
            "deployment_env": self.deployment_env,
            "engine_versions": dict(self.engine_versions or {}),
            "model_resolved": dict(self.model_resolved or {}),
            "model_providers": dict(self.model_providers or {}),
            "sdk_modules": dict(self.sdk_modules or {}),
            "processing_profile": self.processing_profile,
            "feature_flags": dict(self.feature_flags or {}),
            "env_fingerprint": dict(self.env_fingerprint or {}),
            "runtime": dict(self.runtime or {}),
            "captured_at": self.captured_at,
        }


# ─── Public API ──────────────────────────────────────────────────────────────

# Env var names whose values describe the run's tier configuration.
# Captured verbatim so a replay knows which tier ladder was active.
# Secrets (API keys) are masked at capture time — never persisted.
_TIER_ENV_PREFIXES = ("EYES_VISION_TIER", "BRAIN_TIER", "HEART_TIER")
_SECRET_ENV_HINTS = ("API_KEY", "SECRET", "PASSWORD", "TOKEN")


# Boolean-shaped feature flags we care about for reproducibility.
_TRACKED_FEATURE_FLAGS = (
    "EYES_PER_FRAME_LLAVA",
    "EYES_PER_FRAME_OCR",
    "EYES_PHASE2_GUARDS",
    "EYES_TRANSITION_LLM",
    "EYES_AGGRESSIVE_FRAME_EVICTION",
    "EYES_STATE_KEY_MERGE",
    "EYES_ADAPTIVE_SAMPLING",
    "NEXUS_ALLOW_DEGRADED_MODE",
)


def capture_run_provenance(
    *,
    artifact_id: str,
    chain_id: str,
    chain_version: str = "1",
    processing_profile: str = "",
    engine_versions: Optional[dict[str, str]] = None,
    model_resolved: Optional[dict[str, str]] = None,
    model_providers: Optional[dict[str, str]] = None,
    feature_flags: Optional[dict[str, bool]] = None,
    extra_runtime: Optional[dict[str, str]] = None,
) -> PipelineRunProvenance:
    """Build a :class:`PipelineRunProvenance` for the current process.

    Caller-supplied ``engine_versions`` / ``model_resolved`` /
    ``model_providers`` should reflect the actual runtime values the
    chain just used — these can't be inferred reliably from env alone
    because cloud providers may have rotated model ids.  Whatever the
    caller doesn't supply is filled in from environment.
    """
    pipeline_version = _pipeline_version_string()
    sdk_version = _sdk_version_string()
    git_commit = _git_commit()
    deployment_env = (
        os.environ.get("NEXUS_ENV")
        or os.environ.get("DEPLOYMENT_ENV")
        or "unknown"
    )

    flags = dict(feature_flags or {})
    for name in _TRACKED_FEATURE_FLAGS:
        if name not in flags and name in os.environ:
            flags[name] = _truthy(os.environ.get(name))

    env_fingerprint = _capture_tier_env(_TIER_ENV_PREFIXES)
    runtime = _capture_runtime(extra_runtime or {})
    sdk_modules = _capture_sdk_modules()

    return PipelineRunProvenance(
        artifact_id=str(artifact_id),
        chain_id=str(chain_id),
        chain_version=str(chain_version),
        pipeline_version=pipeline_version,
        sdk_version=sdk_version,
        git_commit=git_commit,
        deployment_env=deployment_env,
        engine_versions=dict(engine_versions or {}),
        model_resolved=dict(model_resolved or {}),
        model_providers=dict(model_providers or {}),
        sdk_modules=sdk_modules,
        processing_profile=processing_profile or os.environ.get(
            "EYES_DEFAULT_PROCESSING_PROFILE", "",
        ),
        feature_flags=flags,
        env_fingerprint=env_fingerprint,
        runtime=runtime,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


def diff_provenance(a: dict, b: dict) -> dict[str, dict[str, Any]]:
    """Return a structured diff of two provenance dicts.

    Output shape::

        {
          "model_resolved.eyes": {"a": "claude-sonnet-4-5", "b": "claude-sonnet-4-6"},
          "feature_flags.EYES_PER_FRAME_LLAVA": {"a": False, "b": True},
          ...
        }

    Only fields that differ appear in the result.  Useful for the
    operator UI's "what changed between these two artifacts?" view and
    for compliance reports diffing today's pipeline against the version
    that signed off six months ago.
    """
    out: dict[str, dict[str, Any]] = {}
    _diff_walk("", a or {}, b or {}, out)
    return out


# ─── Internal helpers ────────────────────────────────────────────────────────

def _pipeline_version_string() -> str:
    """Coarse version of the pipeline definition itself.

    Sourced from ``NEXUS_PIPELINE_VERSION`` env if set; otherwise
    derived from the SDK version + a small constant tag so swaps
    in the chain-stage layout are visible to operators.
    """
    explicit = os.environ.get("NEXUS_PIPELINE_VERSION")
    if explicit:
        return explicit
    return f"{_sdk_version_string()}+canonical"


def _sdk_version_string() -> str:
    """SDK version — best-effort lookup."""
    try:
        from importlib.metadata import version, PackageNotFoundError
    except ImportError:  # pragma: no cover — Python <3.8
        return "unknown"
    for name in ("nexus-sdk", "nexus_sdk"):
        try:
            return version(name)
        except PackageNotFoundError:
            continue
        except Exception:
            continue
    return "unknown"


def _git_commit() -> Optional[str]:
    """Return the short git SHA when discoverable; None otherwise.

    Many production deployments inject ``GIT_COMMIT`` as an env var so
    we look there first — it is faster and works in stripped-down
    containers where the .git directory has been removed.
    """
    explicit = (
        os.environ.get("GIT_COMMIT")
        or os.environ.get("NEXUS_GIT_COMMIT")
        or os.environ.get("CI_COMMIT_SHA")
    )
    if explicit:
        return explicit[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return None


def _capture_tier_env(prefixes: Iterable[str]) -> dict[str, str]:
    """Capture tier configuration env vars, redacting secrets."""
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if not any(key.startswith(p) for p in prefixes):
            continue
        if any(hint in key.upper() for hint in _SECRET_ENV_HINTS):
            out[key] = _mask_secret(value)
        else:
            out[key] = value
    return out


def _mask_secret(value: str) -> str:
    """Replace a secret with ``<set>`` (or ``<empty>``) so its presence is
    visible to operators but the value never leaves the boundary."""
    if not value:
        return "<empty>"
    return f"<set:{len(value)}-chars>"


def _capture_runtime(extra: dict[str, str]) -> dict[str, str]:
    """Capture the language + container fingerprint."""
    rt = {
        "python": platform.python_version(),
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "interpreter": sys.implementation.name,
    }
    container_id = os.environ.get("HOSTNAME") or os.environ.get("CONTAINER_ID")
    if container_id:
        rt["hostname"] = container_id
    rt.update({k: v for k, v in extra.items() if v is not None})
    return rt


def _capture_sdk_modules() -> dict[str, str]:
    """Best-effort version map for nexus_sdk sub-packages.

    Walks the ``nexus_sdk.*`` sub-packages we care about and reads
    ``__version__`` when defined; falls back to the parent SDK version
    so the diff layer always has something to compare on.
    """
    try:
        import nexus_sdk  # type: ignore
    except ImportError:  # pragma: no cover — defensive
        return {}
    parent_version = getattr(nexus_sdk, "__version__", _sdk_version_string())
    result: dict[str, str] = {"nexus_sdk": str(parent_version)}
    for sub in (
        "evidence", "vision", "audio", "cursor", "elements",
        "dictionary", "provenance", "observability",
    ):
        try:
            mod = __import__(f"nexus_sdk.{sub}", fromlist=[sub])
        except Exception:
            continue
        result[f"nexus_sdk.{sub}"] = str(
            getattr(mod, "__version__", parent_version),
        )
    return result


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _diff_walk(prefix: str, a: Any, b: Any, out: dict[str, dict[str, Any]]) -> None:
    """Recursively diff two JSON-shaped values, recording leaves only."""
    if isinstance(a, dict) or isinstance(b, dict):
        a_dict = a if isinstance(a, dict) else {}
        b_dict = b if isinstance(b, dict) else {}
        keys = set(a_dict.keys()) | set(b_dict.keys())
        for key in keys:
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            _diff_walk(child_prefix, a_dict.get(key), b_dict.get(key), out)
        return
    if a != b:
        out[prefix or "(root)"] = {"a": a, "b": b}

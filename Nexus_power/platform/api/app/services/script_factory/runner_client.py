"""Client for the internal nexus-runner service.

Submits a configured Playwright bundle for server-side execution and awaits the
result. Internal network only (no host exposure); auth via an optional shared
secret. The run itself reports step results back to Nexus via the bundled
vkpower-reporter, so this client only needs the pass/fail summary.
"""

from __future__ import annotations

import os

import httpx

_RUNNER_URL = os.getenv("NEXUS_RUNNER_URL", "http://nexus-runner:4555")
_RUNNER_TOKEN = os.getenv("RUNNER_TOKEN", "")


async def run_suite(files: dict, env: dict, timeout_ms: int = 240000) -> dict:
    """POST the bundle to the runner and await the (long-running) result.

    Returns {status, exit_code, output}. Raises on transport/HTTP error."""
    files = add_legacy_artifact_aliases(dict(files))

    headers = {"Content-Type": "application/json"}
    if _RUNNER_TOKEN:
        headers["X-Runner-Token"] = _RUNNER_TOKEN
    payload = {"files": files, "env": env, "timeout_ms": timeout_ms}
    # Read timeout must outlast the run's hard cap so we get the real verdict —
    # SCALED with timeout_ms (a certification of a 48-case suite legitimately
    # runs far past the old fixed 660s; a fixed read timeout silently truncated
    # long runs into transport errors).
    read_s = max(660.0, timeout_ms / 1000.0 + 60.0)
    timeout = httpx.Timeout(connect=10.0, read=read_s, write=60.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{_RUNNER_URL}/run", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def run_live(files: dict, env: dict, timeout_ms: int = 240000) -> dict:
    """Start a HEADED, VNC-streamed run on the runner and RETURN immediately
    (the runner replies 202 and runs in the background). Raises on transport /
    HTTP error (incl. 409 when a run is already in progress)."""
    headers = {"Content-Type": "application/json"}
    if _RUNNER_TOKEN:
        headers["X-Runner-Token"] = _RUNNER_TOKEN
    payload = {"files": files, "env": env, "timeout_ms": timeout_ms}
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=60.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{_RUNNER_URL}/run-live", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def live_status() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{_RUNNER_URL}/run-live/status")
        resp.raise_for_status()
        return resp.json()


# ─── Auth capture (interactive login → storageState) ──────────────────────────


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if _RUNNER_TOKEN:
        h["X-Runner-Token"] = _RUNNER_TOKEN
    return h


async def auth_capture_start(url: str) -> dict:
    """Start an interactive headed capture session (operator logs in via noVNC).
    Returns 202 {status: capturing}; raises on 409 when busy."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
        resp = await client.post(f"{_RUNNER_URL}/auth-capture/start", json={"url": url}, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def auth_capture_save() -> dict:
    """Save the captured session — returns {status, storage_state: <object>}."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
        resp = await client.post(f"{_RUNNER_URL}/auth-capture/save", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def auth_capture_cancel() -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_RUNNER_URL}/auth-capture/cancel", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def auth_capture_status() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{_RUNNER_URL}/auth-capture/status")
        resp.raise_for_status()
        return resp.json()


async def health() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{_RUNNER_URL}/health")
        resp.raise_for_status()
        return resp.json()


# ── Tier-2 back-compat: legacy artifact-name aliases ─────────────────────────
_LEGACY_ALIASES = (
    ("vkpower.data.json", "nexus.data.json"),
    ("vkpower.auth.json", "nexus.auth.json"),
    ("vkpower.auth.config.json", "nexus.auth.config.json"),
)


def add_legacy_artifact_aliases(files: dict) -> dict:
    """Saved script VERSIONS may still reference the pre-rename artifact names
    inside their stored source. Whenever the new-named artifact exists and any
    project file still mentions the old name, ship the old-named twin too —
    so versioned scripts keep running unmodified. The alias self-retires once
    no source references the old name."""
    try:
        blob = "\n".join(v for v in files.values() if isinstance(v, str))
        for new, old in _LEGACY_ALIASES:
            if new in files and old not in files and old in blob:
                files[old] = files[new]
    except Exception:  # pragma: no cover — aliasing must never break a run
        pass
    return files

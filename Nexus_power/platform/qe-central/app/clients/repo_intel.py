"""QE-Central → repo-intel typed client (design §3.3, CODE P0.2).

repo-intel runs on the internal ``nexus`` network with no external exposure; this
module is the ONLY qe-central seam that talks to it.  The client REPO token is
relayed once and sealed INSIDE repo-intel (its own KMS envelope) — qe-central
never persists the raw token.

Fail-open on intelligence: every call degrades to an honest ``RepoIntelError`` (or
``None`` for the read paths) so a repo-intel outage never blocks a crawl/cycle.
The URL is fixed to the compose service name, overridable via
``QEC_REPO_INTEL_URL`` for tests / alternate topologies.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("QEC_REPO_INTEL_URL", "http://repo-intel:8014").rstrip("/")
_PREFIX = "/api/v1/repo-intel"
_TIMEOUT_S = 30.0


class RepoIntelError(RuntimeError):
    """A non-2xx / transport failure from repo-intel (message is caller-safe)."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = int(status_code)
        self.detail = str(detail)[:500]
        super().__init__(f"repo-intel {self.status_code}: {self.detail}")


class RepoConnection(BaseModel):
    connection_id: str
    provider: str = ""
    project_path: str = ""


class RepoDiffResult(BaseModel):
    app_id: str = ""
    changed_files: list = []
    mapped_atoms: list = []
    stack_supported: bool = False
    reason: str = ""


async def _request(
    method: str, path: str, *, json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict:
    url = f"{_BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.request(method, url, json=json_body, params=params)
    except Exception as exc:  # transport failure — honest error, never raise into a crash
        logger.warning("qec.repo_intel.transport_error", extra={"path": path, "error": str(exc)[:200]})
        raise RepoIntelError(0, f"transport error: {exc}") from exc
    if resp.status_code // 100 != 2:
        detail = ""
        try:
            detail = str(resp.json().get("detail") or "")
        except Exception:
            detail = resp.text[:300]
        raise RepoIntelError(resp.status_code, detail or f"http {resp.status_code}")
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {"result": body}


async def create_connection(
    *, tenant_id: str, provider: str, base_url: str, project_path: str,
    token: str, default_branch: str = "main", app_id: str = "", label: str = "",
) -> RepoConnection:
    """Register + seal a repo connection (repo-intel healthchecks the remote and
    KMS-seals the token).  Raises :class:`RepoIntelError` on failure."""
    body = {
        "tenant_id": tenant_id, "provider": provider, "base_url": base_url,
        "project_path": project_path, "default_branch": default_branch,
        "token": token, "app_id": app_id, "label": label,
    }
    data = await _request("POST", f"{_PREFIX}/connections", json_body=body)
    return RepoConnection(
        connection_id=str(data.get("connection_id") or ""),
        provider=str(data.get("provider") or provider),
        project_path=str(data.get("project_path") or project_path),
    )


async def analyze(
    *, tenant_id: str, connection_id: str, branch: str = "main", token: Optional[str] = None,
) -> dict:
    """Kick off an async universe build (202).  Raises on a bad connection."""
    params: dict = {"tenant_id": tenant_id, "branch": branch}
    if token:
        params["token"] = token
    return await _request(
        "POST", f"{_PREFIX}/connections/{connection_id}/analyze", params=params,
    )


async def get_diff(
    *, tenant_id: str, app_id: str, connection_id: str, old_sha: str, new_sha: str,
) -> Optional[RepoDiffResult]:
    """Changed atoms between two SHAs; ``None`` on any failure (caller fails safe
    to a full cycle — never a false 'no change')."""
    body = {
        "tenant_id": tenant_id, "connection_id": connection_id,
        "old_sha": old_sha, "new_sha": new_sha,
    }
    try:
        data = await _request("POST", f"{_PREFIX}/{app_id}/diff", json_body=body)
    except RepoIntelError as exc:
        logger.info("qec.repo_intel.diff_unavailable", extra={"app_id": app_id, "detail": exc.detail})
        return None
    return RepoDiffResult(**{k: data.get(k) for k in RepoDiffResult.model_fields if k in data})


async def revoke_connection(*, tenant_id: str, connection_id: str) -> None:
    """Best-effort revoke (wipes the sealed token + workdir in repo-intel)."""
    try:
        await _request(
            "DELETE", f"{_PREFIX}/connections/{connection_id}", params={"tenant_id": tenant_id},
        )
    except RepoIntelError as exc:
        logger.warning("qec.repo_intel.revoke_failed", extra={"connection_id": connection_id, "detail": exc.detail})

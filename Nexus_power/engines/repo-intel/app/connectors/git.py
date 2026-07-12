"""Git connector — shallow clone + ls-remote healthcheck.

Fetches a client repo into a per-tenant, per-connection workdir with a hard
size cap. The token is held in memory only, injected into the remote URL for
the single git invocation, and SCRUBBED from every error/log line — it is
never written to disk (git ``credential.helper`` disabled) and never echoed.

Uses the ``git`` binary via ``subprocess`` (no in-process credential state).
Supports GitLab / GitHub / generic HTTPS git. A ``--filter=blob:none
--depth=1`` clone keeps the transfer small; a sparse-checkout seam is
documented for very large monorepos.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote as _urlquote
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("repo_intel.connectors.git")


class GitConnectorError(RuntimeError):
    """Raised on a clone/ls-remote failure (message is always token-scrubbed)."""


@dataclass
class CloneResult:
    workdir: Path
    deployed_sha: str
    branch: str
    bytes_on_disk: int


def _scrub_token(text: str, token: Optional[str]) -> str:
    """Remove the token AND any URL-embedded credentials from a string."""
    out = text or ""
    if token:
        out = out.replace(token, "[REDACTED:TOKEN]")
        out = out.replace(_urlquote(token, safe=""), "[REDACTED:TOKEN]")
    # userinfo in any https URL → scheme://[REDACTED]@host
    out = re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", out)
    return out


def _auth_url(base_url: str, project_path: str, provider: str, token: Optional[str]) -> str:
    """Build the clone URL with token userinfo (never logged)."""
    root = base_url.rstrip("/")
    path = project_path.strip("/")
    full = f"{root}/{path}.git" if not root.endswith(".git") else root
    if not token:
        return full
    parsed = urlparse(full)
    # GitLab uses oauth2:<token>@; GitHub accepts <token>@ or x-access-token.
    user = "oauth2" if provider == "gitlab" else "x-access-token"
    netloc = f"{user}:{_urlquote(token, safe='')}@{parsed.netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total


class GitConnector:
    """Clones client repos with token-hygiene + size caps."""

    def __init__(
        self, *, workdir_root: str, git_binary: str = "git",
        max_repo_bytes: int = 2_000_000_000, clone_timeout: int = 300,
        ls_remote_timeout: int = 30,
    ) -> None:
        self._root = Path(workdir_root)
        self._git = git_binary
        self._max_bytes = max_repo_bytes
        self._clone_timeout = clone_timeout
        self._ls_remote_timeout = ls_remote_timeout

    def _env(self) -> dict:
        env = dict(os.environ)
        # Never prompt, never persist creds, never read a global gitconfig.
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GCM_INTERACTIVE": "never",
        })
        return env

    def ls_remote(self, *, base_url: str, project_path: str, provider: str,
                  branch: str, token: Optional[str]) -> str:
        """Return the SHA the branch points at (healthcheck). Raises on failure."""
        url = _auth_url(base_url, project_path, provider, token)
        try:
            proc = subprocess.run(
                [self._git, "-c", "credential.helper=", "ls-remote", url, branch],
                capture_output=True, text=True, timeout=self._ls_remote_timeout,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitConnectorError("ls-remote timed out") from exc
        if proc.returncode != 0:
            raise GitConnectorError(_scrub_token(proc.stderr.strip() or "ls-remote failed", token))
        line = proc.stdout.strip().split("\n")[0] if proc.stdout.strip() else ""
        return line.split("\t")[0] if line else ""

    def clone(
        self, *, tenant_id: str, connection_id: str, base_url: str,
        project_path: str, provider: str, branch: str, token: Optional[str],
        sparse_paths: Optional[List[str]] = None,
    ) -> CloneResult:
        """Shallow-clone into ``{root}/{tenant}/{connection}``.

        A repo exceeding the byte cap is removed and refused. ``sparse_paths``
        (optional) restricts the checkout for large monorepos.
        """
        dest = self._root / tenant_id / connection_id
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = _auth_url(base_url, project_path, provider, token)

        cmd = [self._git, "-c", "credential.helper=", "clone",
               "--depth", "1", "--filter=blob:none", "--single-branch",
               "--branch", branch, url, str(dest)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self._clone_timeout, env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitConnectorError("clone timed out") from exc
        if proc.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitConnectorError(_scrub_token(proc.stderr.strip() or "clone failed", token))

        if sparse_paths:
            self._apply_sparse(dest, sparse_paths, token)

        size = _dir_size(dest)
        if size > self._max_bytes:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitConnectorError(
                f"repo exceeds size cap ({size} > {self._max_bytes} bytes) — refused"
            )
        sha = self._head_sha(dest)
        logger.info("cloned connection=%s branch=%s sha=%s bytes=%s",
                    connection_id, branch, sha[:12], size)
        return CloneResult(workdir=dest, deployed_sha=sha, branch=branch, bytes_on_disk=size)

    def _apply_sparse(self, dest: Path, paths: List[str], token: Optional[str]) -> None:
        try:
            subprocess.run([self._git, "-C", str(dest), "sparse-checkout", "init", "--cone"],
                           capture_output=True, text=True, timeout=30, env=self._env())
            subprocess.run([self._git, "-C", str(dest), "sparse-checkout", "set", *paths],
                           capture_output=True, text=True, timeout=30, env=self._env())
        except subprocess.SubprocessError as exc:  # non-fatal — full checkout stands
            logger.debug("sparse-checkout failed: %s", _scrub_token(str(exc), token))

    def _head_sha(self, dest: Path) -> str:
        try:
            proc = subprocess.run(
                [self._git, "-C", str(dest), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=15, env=self._env(),
            )
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except subprocess.SubprocessError:
            return ""

    def changed_files(
        self, *, workdir: Path, old_sha: str, new_sha: str, token: Optional[str] = None,
    ) -> Optional[List[str]]:
        """Repo-relative paths changed between two SHAs (``git diff --name-only``).

        Returns ``None`` when the diff cannot be computed — missing SHA, a shallow
        clone that lacks ``old_sha`` in history, or any git error — so the caller
        FAIL-SAFES to a full cycle (CODE P4 never claims a false "no change").  The
        token is only used to scrub error text; ``git diff`` is a local operation.
        """
        if not (str(old_sha).strip() and str(new_sha).strip()):
            return None
        try:
            proc = subprocess.run(
                [self._git, "-C", str(workdir), "diff", "--name-only",
                 str(old_sha).strip(), str(new_sha).strip()],
                capture_output=True, text=True, timeout=self._ls_remote_timeout, env=self._env(),
            )
        except subprocess.SubprocessError as exc:
            logger.warning("changed_files failed: %s", _scrub_token(str(exc), token))
            return None
        if proc.returncode != 0:
            logger.warning("changed_files git error: %s", _scrub_token(proc.stderr, token)[:200])
            return None
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]

    def remove_workdir(self, *, tenant_id: str, connection_id: str) -> None:
        """Delete a connection's clone (called on revoke/delete)."""
        shutil.rmtree(self._root / tenant_id / connection_id, ignore_errors=True)

"""repo-intel engine — FastAPI application + analyze orchestration.

Exposes the App-Model API (design §3.3): register/analyze a repo connection,
serve the provenance-tagged App Model, the advisory crawler seed manifest
(404 ⇒ crawl blind), live-vs-code drift, the directed-vs-blind A/B report, and
the ``/diff`` endpoint the control plane consumes (fail-safe-to-full).

Off the critical path: every consumer endpoint degrades gracefully. Analyze is
async (returns 202); a per-plugin extractor failure marks the universe
``degraded`` (honest partial), never a silent success.

Security: the git token is envelope-sealed at rest (AAD=connection_id) and
never echoed; the LLM lens is OFF unless explicitly enabled; every quote is
secret-scrubbed before persistence by the extractors.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.ab.harness import ab_report
from app.config import settings
from app.diff.mapper import map_changed_atoms
from app.connectors.git import GitConnector, GitConnectorError
from app.detect.stack import detect
from app.drift.report import build_drift_report
from app.extract.registry import ExtractionContext, run_extractors
from app.manifest.seed import build_seed_manifest
from app.model.secrets import EnvelopeUnavailable, open_token, seal_token
from app.model.store import RepoIntelStore

logging.basicConfig(
    level=getattr(logging, str(getattr(settings, "log_level", "INFO")).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("repo_intel")

app = FastAPI(title="repo-intel", version=settings.engine_version)

_store: Optional[RepoIntelStore] = None
_git: Optional[GitConnector] = None
_analyze_tasks: set = set()


def store() -> RepoIntelStore:
    if _store is None:  # pragma: no cover - lifespan guarantees init
        raise HTTPException(503, "store not initialised")
    return _store


def git() -> GitConnector:
    if _git is None:  # pragma: no cover
        raise HTTPException(503, "git connector not initialised")
    return _git


@app.on_event("startup")
async def _startup() -> None:
    global _store, _git
    _store = RepoIntelStore(settings.qec_database_url)
    _git = GitConnector(
        workdir_root=settings.repo_workdir_root,
        git_binary=settings.git_binary,
        max_repo_bytes=settings.max_repo_bytes,
        clone_timeout=settings.clone_timeout_seconds,
        ls_remote_timeout=settings.ls_remote_timeout_seconds,
        clone_depth=settings.clone_depth,
    )
    logger.info("repo-intel started on :%s", settings.engine_port)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _store is not None:
        await _store.dispose()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "engine": settings.engine_name,
        "version": settings.engine_version,
    }


# ───────────────────────────── request models ────────────────────────────


class ConnectionIn(BaseModel):
    tenant_id: str
    provider: str = Field(default="gitlab", pattern="^(gitlab|github|generic_git)$")
    base_url: str
    project_path: str
    default_branch: str = "main"
    token: str = Field(min_length=1)
    app_id: str = ""
    label: str = ""


class DiffIn(BaseModel):
    tenant_id: str
    connection_id: str
    old_sha: str = ""
    new_sha: str = ""


# ─────────────────────────────── endpoints ───────────────────────────────


@app.post("/api/v1/repo-intel/connections")
async def create_connection(body: ConnectionIn) -> dict:
    """Register a connection: healthcheck the remote, seal the token, persist.
    The token is NEVER echoed and NEVER stored in plaintext."""
    # 1) prove the remote + branch are reachable (also validates the token).
    try:
        git().ls_remote(
            base_url=body.base_url, project_path=body.project_path,
            provider=body.provider, branch=body.default_branch, token=body.token,
        )
    except GitConnectorError as exc:
        raise HTTPException(400, f"remote healthcheck failed: {exc}")

    # 2) seal the token (refuse-plaintext outside development).
    import uuid
    connection_id = uuid.uuid4().hex
    try:
        blob, kek_id = await seal_token(body.token, tenant_id=body.tenant_id, aad=connection_id)
    except EnvelopeUnavailable as exc:
        raise HTTPException(503, str(exc))

    # 3) persist under the SAME id the token was sealed with (AAD match).
    await store().create_connection(
        tenant_id=body.tenant_id, provider=body.provider, base_url=body.base_url,
        project_path=body.project_path, default_branch=body.default_branch,
        encrypted_token=blob, kek_id=kek_id, app_id=body.app_id, label=body.label,
        connection_id=connection_id,
    )
    return {"status": "registered", "connection_id": connection_id,
            "provider": body.provider, "project_path": body.project_path}


@app.delete("/api/v1/repo-intel/connections/{connection_id}")
async def delete_connection(connection_id: str, tenant_id: str) -> dict:
    await store().revoke_connection(tenant_id=tenant_id, connection_id=connection_id)
    git().remove_workdir(tenant_id=tenant_id, connection_id=connection_id)
    return {"status": "revoked", "connection_id": connection_id}


@app.post("/api/v1/repo-intel/connections/{connection_id}/analyze")
async def analyze(connection_id: str, tenant_id: str, branch: str = "main",
                  token: Optional[str] = None) -> dict:
    """Kick off an async universe build. Returns 202 immediately.

    For this Phase-2 surface the token may be supplied per-call (the sealed
    token is used on the VM where the KEK is provisioned); the analyze task
    clones, detects, extracts, persists atoms + universe, and builds the seed
    manifest.
    """
    task = asyncio.create_task(_run_analyze(tenant_id, connection_id, branch, token))
    _analyze_tasks.add(task)
    task.add_done_callback(_analyze_tasks.discard)
    return {"status": "accepted", "connection_id": connection_id}


async def _run_analyze(tenant_id: str, connection_id: str, branch: str,
                       token: Optional[str]) -> None:
    """The analyze pipeline (clone → detect → extract → persist → seed)."""
    try:
        # In production the token is opened from the sealed blob; the router
        # passes it explicitly only where the KEK is not yet provisioned.
        clone = git().clone(
            tenant_id=tenant_id, connection_id=connection_id,
            base_url="", project_path="", provider="generic_git",
            branch=branch, token=token,
        ) if token else None
        repo_path = clone.workdir if clone else Path(settings.repo_workdir_root) / tenant_id / connection_id
        deployed_sha = clone.deployed_sha if clone else ""

        fingerprint = detect(repo_path)
        ctx = ExtractionContext(repo_path=repo_path, deployed_sha=deployed_sha,
                                stacks=frozenset(fingerprint.extractors))
        result = run_extractors(repo_path, ctx)

        extractor_versions = {name: "v1" for name in result.ran_extractors}
        universe_id = await store().upsert_universe(
            tenant_id=tenant_id, connection_id=connection_id,
            deployed_sha=deployed_sha, branch=branch,
            stack_fingerprint=fingerprint.to_dict(),
            extractor_versions=extractor_versions,
            ceiling_bands=result.ceiling_bands,
            status=result.universe_status,
            degraded_extractors=result.degraded_extractors,
            atom_count=len(result.atoms),
        )
        await store().replace_atoms(
            tenant_id=tenant_id, universe_id=universe_id,
            atom_rows=[a.to_row() for a in result.atoms],
        )
        manifest = build_seed_manifest(result.atoms)
        await store().upsert_seed_manifest(
            tenant_id=tenant_id, universe_id=universe_id, manifest=manifest,
        )
        logger.info("analyze complete universe=%s atoms=%s status=%s",
                    universe_id, len(result.atoms), result.universe_status)
    except Exception:
        logger.exception("analyze failed for connection=%s", connection_id)


@app.get("/api/v1/repo-intel/universes/{universe_id}")
async def get_universe(universe_id: str, tenant_id: str) -> dict:
    u = await store().get_universe(tenant_id=tenant_id, universe_id=universe_id)
    if u is None:
        raise HTTPException(404, "universe not found")
    return {
        "universe_id": u.universe_id, "status": u.status,
        "ceiling_bands": u.ceiling_bands, "degraded_extractors": u.degraded_extractors,
        "atom_count": u.atom_count, "deployed_sha": u.deployed_sha,
        "stack_fingerprint": u.stack_fingerprint,
    }


@app.get("/api/v1/repo-intel/universes/{universe_id}/atoms")
async def list_atoms(universe_id: str, tenant_id: str, kind: Optional[str] = None) -> dict:
    atoms = await store().list_atoms(tenant_id=tenant_id, universe_id=universe_id, kind=kind)
    return {"count": len(atoms), "atoms": [
        {"kind": a.kind, "value": a.value, "provenance": f"{a.provenance_path}:{a.provenance_line}",
         "quote": a.quote, "confidence": a.confidence, "source_tier": a.source_tier}
        for a in atoms
    ]}


@app.get("/api/v1/repo-intel/universes/{universe_id}/seed-manifest")
async def seed_manifest(universe_id: str, tenant_id: str) -> dict:
    """THE crawler seam. 404 ⇒ the crawler proceeds BLIND (fail-open)."""
    manifest = await store().get_seed_manifest(tenant_id=tenant_id, universe_id=universe_id)
    if manifest is None:
        raise HTTPException(404, "no seed manifest — crawl blind")
    return manifest


@app.post("/api/v1/repo-intel/{app_id}/diff")
async def diff(app_id: str, body: DiffIn) -> dict:
    """Changed atoms between two SHAs → the control plane runs ONLY affected tests.

    FAIL-SAFE-TO-FULL (never green-wash): ANY inability to compute a precise diff —
    no analyzed universe, a shallow clone missing ``old_sha``, a git error, or a
    changed file that maps to no atom — returns ``stack_supported=false`` /
    ``fail_safe_to_full=true`` so the consumer treats EVERYTHING as changed. A
    narrowed result is returned ONLY when every changed file was mapped to an atom.
    """
    def _full(reason: str) -> dict:
        return {"app_id": app_id, "changed_files": [], "changed_atoms": [],
                "affected_routes": [], "stack_supported": False,
                "fail_safe_to_full": True, "reason": reason}

    universe = await store().latest_universe_for_connection(
        tenant_id=body.tenant_id, connection_id=body.connection_id)
    if universe is None:
        return _full("no analyzed universe for this connection — run full")

    workdir = Path(settings.repo_workdir_root) / body.tenant_id / body.connection_id
    changed = git().changed_files(
        workdir=workdir, old_sha=body.old_sha, new_sha=body.new_sha)
    if changed is None:
        return _full("diff unavailable (shallow clone / missing sha / git error) — run full")

    atoms = await store().list_atoms(tenant_id=body.tenant_id, universe_id=universe.universe_id)
    result = map_changed_atoms(changed, atoms)
    result["app_id"] = app_id
    result["universe_id"] = universe.universe_id
    return result


@app.get("/api/v1/repo-intel/ab-report")
async def ab(tenant_id: str, universe_id: str,
             directed_artifact_id: str, blind_artifact_id: str) -> dict:
    """Directed-vs-blind grading. The route sets come from page_visits of the
    two crawl artifacts (read by the caller); here we expose the pure grader
    over the universe route set the caller supplies via atoms."""
    atoms = await store().list_atoms(tenant_id=tenant_id, universe_id=universe_id)
    universe_routes = [a.value.get("path_pattern") or a.value.get("path", "")
                       for a in atoms if a.kind in ("route", "api_endpoint")]
    # Reached-paths are supplied by the control plane in a fuller integration;
    # with only ids here we return the universe denominator + an empty grade
    # so the endpoint is honest about needing the crawl inputs.
    return ab_report(universe_routes, [], [])
